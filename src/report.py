"""Daily report, sent at 17:10 ET after the CME session closes (Mon-Fri)."""
import datetime as dt
import json

from boto3.dynamodb.conditions import Key

import core
import watchdog


def summarize(trades):
    pnls = [float(x["pnl"]) for x in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    peak = curve = drawdown = 0.0
    for p in pnls:  # trades come back sorted by close time
        curve += p
        peak = max(peak, curve)
        drawdown = min(drawdown, curve - peak)
    return {
        "trades": len(pnls), "wins": len(wins), "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(pnls), 1) if pnls else None,
        "net": round(sum(pnls), 2), "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "max_drawdown": round(drawdown, 2),
    }


def money(v):
    return "—" if v is None else f"{'+' if v > 0 else ''}{v:,.2f}"


def handler(event, _ctx):
    t = core.now()
    day = (event or {}).get("day") or core.trading_day(t - dt.timedelta(minutes=30))
    states = core.states()
    out = {}
    for raw in states:
        state = core.from_ddb(raw)
        bot = state["bot"]
        trades = core.from_ddb(core.table().query(KeyConditionExpression=Key("pk").eq(f"TRADES#{bot}#{day}"))["Items"])
        s = summarize(trades)
        a = watchdog.assess(state, t)
        lines = [
            f"Trading day {day}",
            f"Net P&L: {money(s['net'])}   ({s['trades']} trades, {s['wins']}W / {s['losses']}L"
            + (f", {s['win_rate']}% win rate)" if s["win_rate"] is not None else ")"),
            f"Best {money(s['best'])}   Worst {money(s['worst'])}   Max drawdown {money(s['max_drawdown'])}",
            f"Avg win {money(s['avg_win'])}   Avg loss {money(s['avg_loss'])}",
            f"Equity: {money(state.get('equity'))}   Open positions: {state.get('open_positions', '—')}",
            f"Last heartbeat: {a['heartbeat_age_min']} min ago" if a["heartbeat_age_min"] is not None else "Last heartbeat: never",
        ]
        core.notify(f"📊 {bot.upper()} daily report {money(s['net'])}", lines, bot=bot, kind="report")
        core.s3().put_object(Bucket=core.bucket(), Key=f"reports/bot={bot}/day={day}.json",
                             Body=json.dumps({"day": day, **s}).encode(), ContentType="application/json")
        out[bot] = s
    return out
