"""Runs every minute. The dead-man switch: silence is the alarm.

Alerts fire once when something breaks and once when it recovers - no spam.
  DOWN   no heartbeat for HEARTBEAT_TIMEOUT_MIN while the market is open
  STALE  heartbeats arrive but market data stopped moving for STALE_MIN
  LOSS   day P&L crossed -DAILY_LOSS_ALERT (one alert per trading day)
"""
import json
import os

import core


ANT_FAIL_ALERT = 3


def minutes(a, b):
    return round((a - b).total_seconds() / 60, 1)


def assess(state, t):
    hb = core.parse_ts(state.get("last_heartbeat"))
    tick = core.parse_ts(state.get("last_tick"))
    hb_age = minutes(t, hb) if hb else None
    tick_age = minutes(t, tick) if tick else None
    is_open = core.market_open(t)
    timeout = float(os.environ.get("HEARTBEAT_TIMEOUT_MIN", "5"))
    stale = float(os.environ.get("STALE_MIN", "3"))
    loss_limit = float(os.environ.get("DAILY_LOSS_ALERT", "0"))

    down = is_open and (hb_age is None or hb_age > timeout)
    # Staleness is judged at the moment of the last heartbeat: "alive, but looking at old prices".
    # (Measuring against now would make every late heartbeat look like stale data before DOWN fires.)
    tick_lag = minutes(hb, tick) if hb and tick else None
    stale_data = is_open and not down and tick_lag is not None and tick_lag > stale
    day_pnl = state.get("day_pnl")
    loss = bool(loss_limit) and day_pnl is not None and day_pnl <= -loss_limit

    health = "down" if down else "stale" if stale_data else "warning" if loss else "ok" if is_open else "market closed"
    return {"bot": state.get("bot"), "health": health, "heartbeat_age_min": hb_age, "tick_age_min": tick_age, "tick_lag_min": tick_lag,
            "day_pnl": day_pnl, "equity": state.get("equity"), "open_positions": state.get("open_positions"),
            "version": state.get("version"), "ants": state.get("ants"), "paused": state.get("paused"),
            "drawdown": state.get("drawdown"), "_down": down, "_stale": stale_data, "_loss": loss}


def set_flags(bot, **flags):
    names = {f"#{k}": k for k in flags}
    values = {f":{k}": v for k, v in flags.items()}
    core.table().update_item(Key=core.state_key(bot),
                             UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in flags),
                             ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def check(state, t):
    a = assess(state, t)
    bot, fired = a["bot"], []
    open_note = f"Open positions: {a['open_positions']}" if a["open_positions"] is not None else "Open positions: unknown"

    if a["_down"] and not state.get("alert_down"):
        core.notify(f"🔴 {bot.upper()} DOWN", [
            f"No heartbeat for {a['heartbeat_age_min'] if a['heartbeat_age_min'] is not None else 'ever'} min while the market is open.",
            open_note, "Check the bot host and the broker platform now."], bot=bot, kind="down")
        set_flags(bot, alert_down=True)
        fired.append("down")
    elif not a["_down"] and state.get("alert_down"):
        core.notify(f"🟢 {bot.upper()} back", [f"Heartbeat restored ({a['heartbeat_age_min']} min ago)."], bot=bot, kind="recovered")
        set_flags(bot, alert_down=False)
        fired.append("recovered")

    if a["_stale"] and not state.get("alert_stale"):
        core.notify(f"🟡 {bot.upper()} stale data", [
            f"Bot is alive but its last market tick was {a['tick_lag_min']} min old when it checked in.",
            "The data feed may be frozen - the bot could be trading on old prices.", open_note], bot=bot, kind="stale")
        set_flags(bot, alert_stale=True)
        fired.append("stale")
    elif not a["_stale"] and state.get("alert_stale"):
        core.notify(f"🟢 {bot.upper()} data flowing", ["Market data is fresh again."], bot=bot, kind="fresh")
        set_flags(bot, alert_stale=False)
        fired.append("fresh")

    # the bot's own workers: an ant stuck failing is caught here even while the bot keeps beating
    failing = sorted(x["name"] for x in (state.get("ants") or []) if x.get("consecutive_failures", 0) >= ANT_FAIL_ALERT)
    was = set(state.get("alert_ants") or [])
    for name in failing:
        if name not in was:
            ant = next(x for x in state["ants"] if x["name"] == name)
            core.notify(f"🐜 {bot.upper()} ant '{name}' failing", [
                f"The '{name}' ant has failed {ant['consecutive_failures']} times in a row (its Queen keeps restarting it).",
                f"Last error: {ant.get('last_error') or 'unknown'}"], bot=bot, kind="ant")
            fired.append(f"ant:{name}")
    for name in sorted(was - set(failing)):
        core.notify(f"🐜 {bot.upper()} ant '{name}' back to work", [f"The '{name}' ant is running cleanly again."],
                    bot=bot, kind="recovered")
        fired.append(f"ant-ok:{name}")
    if set(failing) != was:
        set_flags(bot, alert_ants=failing)

    # account drawdown from its best balance (Lucid closes the account at $1,000): warn before the hard line
    dd_line = float(os.environ.get("DRAWDOWN_ALERT", "0") or 0)
    dd = state.get("drawdown")
    if dd_line and dd is not None:
        if dd >= dd_line and not state.get("alert_drawdown"):
            core.notify(f"🟠 {bot.upper()} drawdown {dd:,.0f}", [
                f"The account is ${dd:,.2f} below its best balance, past your warning line of ${dd_line:,.0f}.",
                "Lucid Flex closes the account at $1,000 below the best balance.", open_note], bot=bot, kind="loss")
            set_flags(bot, alert_drawdown=True)
            fired.append("drawdown")
        elif dd < dd_line * .9 and state.get("alert_drawdown"):      # 10% hysteresis so it can't flap
            core.notify(f"🟢 {bot.upper()} drawdown back under the line", [f"Drawdown is now ${dd:,.2f}."],
                        bot=bot, kind="recovered")
            set_flags(bot, alert_drawdown=False)
            fired.append("drawdown-ok")

    day = core.trading_day(t)
    if a["_loss"] and state.get("alert_loss_day") != day:
        core.notify(f"🟠 {bot.upper()} loss limit", [
            f"Day P&L is {a['day_pnl']:,.2f}, past the alert line of -{float(os.environ['DAILY_LOSS_ALERT']):,.2f}.",
            "Check this before the prop firm's own daily limit is hit.", open_note], bot=bot, kind="loss")
        set_flags(bot, alert_loss_day=day)
        fired.append("loss")
    return fired


def handler(_event, _ctx):
    t = core.now()
    items = core.states()
    result = {core.from_ddb(i)["bot"]: check(core.from_ddb(i), t) for i in items}
    print(json.dumps({"checked": core.iso(t), "market_open": core.market_open(t), "fired": result}))
    return result
