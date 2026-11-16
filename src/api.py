"""Ingest API. The bot calls this; nothing else should.

POST /heartbeat  {"bot": "atlas", "last_tick": <iso|epoch>, "equity": 50210.5, "day_pnl": 210.5, "open_positions": 1}
           optional market fields: "symbol": "MNQZ6", "price": 21012.5, "bid": 21012.25, "ask": 21012.5,
           "bars": [{"t": <minute start>, "o":..., "h":..., "l":..., "c":..., "v":...}]   (up to 10 closed 1-min bars)
POST /trade      {"bot": "atlas", "id": "T-1042", "symbol": "MNQZ6", "side": "long", "qty": 2,
                  "entry": 21010.25, "exit": 21022.75, "pnl": 50.0, "opened_at": ..., "closed_at": ...}
POST /market     {"bot": "atlas", "symbol": "MNQ12-26", "price": ..., "bid": ..., "ask": ..., "bars": [...]}
                 price feed only (e.g. from the NinjaTrader add-on). It never counts as a heartbeat, so a live
                 chart can't hide a dead bot from the dead-man switch.
POST /market     {"bot": "atlas", "symbol": "MESZ6", "price": ..., "bid": ..., "ask": ..., "bars": [...up to 60 1-min bars]}
                 market data only - it never counts as a heartbeat, so a live price feed can't hide a dead bot
GET  /status     current health of every bot
GET  /feed       one bot's day: state, trades, heartbeat samples, alert log (powers the dashboard)
GET  /           the dashboard page itself (public HTML; it asks for the key and sends it on every call)

Every request except GET / needs header  x-atlas-key: <key from SSM>.
"""
import hmac
import os
import json
import pathlib
import re

from boto3.dynamodb.conditions import Key

import core

BOT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TRADE_FIELDS = {"id", "symbol", "side", "qty", "pnl"}
TTL_DAYS = 90
BEAT_TTL_DAYS = 7
BAR_TTL_DAYS = 14
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._:-]{0,19}$")
MAX_BARS = 10
PAGE = pathlib.Path(__file__).with_name("index.html")


def resp(code, body):
    return {"statusCode": code, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(body, default=str)}


def authorized(event):
    expected = core.secret("INGEST_KEY_PARAM") or ""
    got = (event.get("headers") or {}).get("x-atlas-key", "")
    return bool(expected) and hmac.compare_digest(got.encode(), expected.encode())


def body_of(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    bot = str(data.get("bot", "")).lower()
    if not BOT_RE.match(bot):
        raise ValueError("bot must be 1-32 chars: a-z 0-9 _ -")
    data["bot"] = bot
    return data


def num(v, name):
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")


def heartbeat(data):
    t = core.now()
    names, values = ["#hb", "#bot"], {":hb": core.iso(t), ":bot": data["bot"]}
    sets = ["#hb = :hb", "#bot = :bot"]
    for field in ("equity", "day_pnl", "open_positions"):
        if field in data:
            sets.append(f"#{field} = :{field}")
            names.append(f"#{field}")
            values[f":{field}"] = core.to_ddb(num(data[field], field))
    if data.get("last_tick") not in (None, ""):
        sets.append("#tick = :tick")
        names.append("#tick")
        values[":tick"] = core.iso(core.parse_ts(data["last_tick"]))
    if data.get("version"):
        sets.append("#ver = :ver")
        names.append("#ver")
        values[":ver"] = str(data["version"])[:40]
    extra = colony_fields(data)
    for k, v in extra.items():
        sets.append(f"#{k} = :{k}")
        names.append(f"#{k}")
        values[f":{k}"] = v
    market = market_fields(data, t)
    for k, v in market["state"].items():
        sets.append(f"#{k} = :{k}")
        names.append(f"#{k}")
        values[f":{k}"] = v
    attr_names = {n: {"#hb": "last_heartbeat", "#bot": "bot", "#tick": "last_tick", "#ver": "version"}.get(n, n[1:])
                  for n in names}
    sample = {k: values[f":{k}"] for k in ("equity", "day_pnl", "open_positions", "price") if f":{k}" in values}
    if ":tick" in values:
        sample["tick_lag"] = core.to_ddb(round((t - core.parse_ts(values[":tick"])).total_seconds(), 1))
    core.table().put_item(Item={"pk": f"BEATS#{data['bot']}#{core.trading_day(t)}", "sk": core.iso(t), **sample,
                                "expires": int(t.timestamp()) + BEAT_TTL_DAYS * 86400})
    core.table().update_item(
        Key=core.state_key(data["bot"]),
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=values,
    )
    save_market(data["bot"], market)
    return resp(200, {"ok": True, "received": core.iso(t), "bars": len(market["bars"])})


def market(data):
    t = core.now()
    m = market_fields(data, t)
    if not m["state"]:
        raise ValueError("symbol is required")
    m["state"]["market_at"] = core.iso(t)
    core.table().update_item(
        Key=core.state_key(data["bot"]),
        UpdateExpression="SET #bot = :bot, " + ", ".join(f"#{k} = :{k}" for k in m["state"]),
        ExpressionAttributeNames={"#bot": "bot", **{f"#{k}": k for k in m["state"]}},
        ExpressionAttributeValues={":bot": data["bot"], **{f":{k}": v for k, v in m["state"].items()}},
    )
    for bar in m["bars"]:
        core.table().put_item(Item=bar)
    return resp(200, {"ok": True, "bars": len(m["bars"])})


def colony_fields(data):
    """Optional: the bot's own view of itself. Atlas sends each ant's health from its Queen,
    whether it's paused, and its account drawdown."""
    out = {}
    ants = data.get("ants")
    if ants is not None:
        if not isinstance(ants, list) or len(ants) > 40:
            raise ValueError("ants must be a list of at most 40")
        clean = []
        for a in ants:
            if not isinstance(a, dict) or not BOT_RE.match(str(a.get("name", "")).lower()):
                raise ValueError("each ant needs a simple name")
            clean.append({"name": str(a["name"]).lower(),
                          "runs": int(num(a.get("runs", 0), "runs")), "failures": int(num(a.get("failures", 0), "failures")),
                          "consecutive_failures": int(num(a.get("consecutive_failures", 0), "consecutive_failures")),
                          "last_success": str(a["last_success"])[:40] if a.get("last_success") else None,
                          "last_error": str(a["last_error"])[:160] if a.get("last_error") else None})
        out["ants"] = clean
    if "paused" in data:
        out["paused"] = bool(data["paused"])
    if data.get("drawdown") is not None:
        out["drawdown"] = core.to_ddb(num(data["drawdown"], "drawdown"))
    return out


def market_fields(data, t, max_bars=MAX_BARS):
    """Optional live-market fields the bot can attach to a heartbeat: what it sees, as it sees it."""
    out = {"state": {}, "bars": []}
    if not any(k in data for k in ("symbol", "price", "bid", "ask", "bars")):
        return out
    symbol = str(data.get("symbol", "")).upper()
    if not SYMBOL_RE.match(symbol):
        raise ValueError("symbol is required with market data (A-Z 0-9 . _ : -, max 20)")
    out["state"]["symbol"] = symbol
    for k in ("price", "bid", "ask"):
        if data.get(k) is not None:
            out["state"][k] = core.to_ddb(num(data[k], k))
    if "price" in out["state"]:
        out["state"]["price_at"] = core.iso(core.parse_ts(data.get("last_tick")) or t)
    bars = data.get("bars") or []
    if not isinstance(bars, list) or len(bars) > max_bars:
        raise ValueError(f"bars must be a list of at most {max_bars}")
    for b in bars:
        if not isinstance(b, dict):
            raise ValueError("each bar must be an object")
        bt = core.parse_ts(b.get("t"))
        if bt is None:
            raise ValueError("bar needs t")
        bt = bt.replace(second=0, microsecond=0)
        o, h, l, c = (num(b.get(k), k) for k in ("o", "h", "l", "c"))
        if not (l <= min(o, c) and h >= max(o, c)):
            raise ValueError("bar high/low don't contain open/close")
        out["bars"].append(core.to_ddb({"pk": f"BARS#{data['bot']}#{symbol}#{core.trading_day(bt)}", "sk": core.iso(bt),
                                        "o": o, "h": h, "l": l, "c": c, "v": num(b.get("v", 0), "v"),
                                        "expires": int(bt.timestamp()) + BAR_TTL_DAYS * 86400}))
    return out


def save_market(bot, market):
    for bar in market["bars"]:
        core.table().put_item(Item=bar)  # same minute again = overwrite, so re-sent bars are harmless
    q = {k: v for k, v in market["state"].items() if k != "symbol"}
    if "symbol" in market["state"]:  # merge, so a bars-only message keeps the last known price
        key = {"pk": f"QUOTES#{bot}", "sk": market["state"]["symbol"]}
        if q:
            core.table().update_item(Key=key, UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in q),
                                     ExpressionAttributeNames={f"#{k}": k for k in q},
                                     ExpressionAttributeValues={f":{k}": v for k, v in q.items()})
        else:
            core.table().update_item(Key=key, UpdateExpression="SET #seen = :seen",
                                     ExpressionAttributeNames={"#seen": "seen"}, ExpressionAttributeValues={":seen": core.iso(core.now())})


def market(data):
    """Price feed only. Deliberately does NOT touch last_heartbeat: the dead-man switch must watch the bot, not its data."""
    t = core.now()
    if not any(k in data for k in ("price", "bars")):
        raise ValueError("send price and/or bars")
    m = market_fields(data, t, max_bars=60)
    save_market(data["bot"], m)
    return resp(200, {"ok": True, "bars": len(m["bars"])})


def trade(data):
    missing = TRADE_FIELDS - data.keys()
    if missing:
        raise ValueError(f"missing: {', '.join(sorted(missing))}")
    tid = str(data["id"])[:64]
    if not re.match(r"^[A-Za-z0-9._:-]+$", tid):
        raise ValueError("id has odd characters")
    side = str(data["side"]).lower()
    if side not in ("long", "short", "buy", "sell"):
        raise ValueError("side must be long/short/buy/sell")
    closed = core.parse_ts(data.get("closed_at")) or core.now()
    day = core.trading_day(closed)
    record = {
        "bot": data["bot"], "id": tid, "symbol": str(data["symbol"])[:20], "side": side,
        "qty": num(data["qty"], "qty"), "pnl": num(data["pnl"], "pnl"),
        "entry": num(data["entry"], "entry") if "entry" in data else None,
        "exit": num(data["exit"], "exit") if "exit" in data else None,
        "opened_at": core.iso(core.parse_ts(data["opened_at"])) if data.get("opened_at") else None,
        "closed_at": core.iso(closed), "trading_day": day,
    }
    item = core.to_ddb({k: v for k, v in record.items() if v is not None})
    item.update(pk=f"TRADES#{data['bot']}#{day}", sk=f"{record['closed_at']}#{tid}",
                expires=int(closed.timestamp()) + TTL_DAYS * 86400)
    try:
        core.table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    except core.table().meta.client.exceptions.ConditionalCheckFailedException:
        return resp(200, {"ok": True, "duplicate": True})  # retries are safe
    # Permanent, Athena-friendly audit trail. DynamoDB copy expires; this one doesn't.
    core.s3().put_object(Bucket=core.bucket(), Key=f"trades/bot={data['bot']}/day={day}/{tid}.json",
                         Body=json.dumps(record).encode(), ContentType="application/json")
    return resp(201, {"ok": True, "trading_day": day})


def status():
    import watchdog
    items = core.states()
    t = core.now()
    return resp(200, {"checked": core.iso(t), "market_open": core.market_open(t),
                      "bots": [watchdog.assess(core.from_ddb(i), t) for i in items]})


def query_all(pk, **kw):
    items, start = [], None
    while True:
        page = core.table().query(KeyConditionExpression=Key("pk").eq(pk), **kw, **({"ExclusiveStartKey": start} if start else {}))
        items += page["Items"]
        start = page.get("LastEvaluatedKey")
        if not start or kw.get("Limit"):
            return core.from_ddb(items)


def thin(rows, cap=480):
    """Keep the dashboard light: at most `cap` heartbeat samples, always including the last."""
    if len(rows) <= cap:
        return rows
    step = len(rows) / cap
    return [rows[int(i * step)] for i in range(cap - 1)] + [rows[-1]]


def feed(event):
    import report
    import watchdog
    q = event.get("queryStringParameters") or {}
    t = core.now()
    bots = [core.from_ddb(i) for i in core.states()]
    names = sorted(b["bot"] for b in bots)
    bot = (q.get("bot") or "").lower() or ("atlas" if "atlas" in names else names[0] if names else "")
    day = q.get("day") or core.trading_day(t)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise ValueError("day must be YYYY-MM-DD")
    state = next((b for b in bots if b["bot"] == bot), None)
    trades = query_all(f"TRADES#{bot}#{day}") if bot else []
    beats = query_all(f"BEATS#{bot}#{day}") if bot else []
    alerts = [a for a in query_all("ALERTS", ScanIndexForward=False, Limit=60) if a.get("bot") in (bot, "-")]
    quotes = {x["sk"]: x for x in query_all(f"QUOTES#{bot}")} if bot else {}
    source = bot
    if bot and not quotes and "-" in bot:          # e.g. atlas-paper trades the same market atlas streams
        family = bot.split("-")[0]
        quotes = {x["sk"]: x for x in query_all(f"QUOTES#{family}")}
        source = family if quotes else bot
    symbols = sorted(quotes, key=lambda x: (not x.startswith("MNQ"), x))
    symbol = (q.get("symbol") or (state or {}).get("symbol") or (symbols[0] if symbols else "")).upper()
    if symbol and not SYMBOL_RE.match(symbol):
        raise ValueError("bad symbol")
    quote = quotes.get(symbol) or {}
    bars = query_all(f"BARS#{source}#{symbol}#{day}") if bot and symbol else []
    strip = lambda d: {k: v for k, v in d.items() if k not in ("pk", "expires")}
    return resp(200, {
        "now": core.iso(t), "market_open": core.market_open(t), "trading_day": day, "bots": names, "bot": bot,
        "config": {"heartbeat_timeout_min": float(os.environ.get("HEARTBEAT_TIMEOUT_MIN", "5")),
                   "stale_min": float(os.environ.get("STALE_MIN", "3")),
                   "daily_loss_alert": float(os.environ.get("DAILY_LOSS_ALERT", "0")),
                   "drawdown_alert": float(os.environ.get("DRAWDOWN_ALERT", "0"))},
        "state": {k: v for k, v in watchdog.assess(state, t).items() if not k.startswith("_")} if state else None,
        "summary": report.summarize(trades),
        "trades": [strip(x) for x in trades],
        "beats": [{"t": b["sk"], **{k: b[k] for k in ("equity", "day_pnl", "tick_lag", "open_positions") if k in b}}
                  for b in thin(beats)],
        "alerts": [strip(a) for a in alerts][:40],
        "market": {"symbol": symbol or None, "symbols": symbols, "source": source if symbol else None,
                   "quote": {k: quote.get(k) for k in ("price", "bid", "ask", "price_at")} if symbol else None,
                   "bars": [{"t": b["sk"], **{k: b[k] for k in ("o", "h", "l", "c", "v")}} for b in bars[-1440:]]},
    })


def page():
    return {"statusCode": 200, "body": PAGE.read_text(encoding="utf-8"),
            "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-cache",
                        "x-frame-options": "DENY", "referrer-policy": "no-referrer",
                        "content-security-policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                        "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"}}


def handler(event, _ctx):
    route = event.get("routeKey", "")
    if route == "GET /":
        return page()
    if not authorized(event):
        return resp(401, {"error": "unauthorized"})
    route = event.get("routeKey", "")
    try:
        if route == "POST /heartbeat":
            return heartbeat(body_of(event))
        if route == "POST /market":
            return market(body_of(event))
        if route == "POST /market":
            return market(body_of(event))
        if route == "POST /trade":
            return trade(body_of(event))
        if route == "GET /status":
            return status()
        if route == "GET /feed":
            return feed(event)
        return resp(404, {"error": "no such route"})
    except (ValueError, json.JSONDecodeError) as e:
        return resp(400, {"error": str(e)})
