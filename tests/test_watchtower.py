"""Watchtower tests. No network, no AWS: DynamoDB, S3, SNS and SSM are moto; the clock is frozen per test."""
import datetime as dt
import importlib
import json
import pathlib
import sys

import boto3
import pytest
from moto import mock_aws

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
KEY = "test-key-123"
UTC = dt.timezone.utc
# Wednesday 2026-11-18, 14:00 UTC = 9:00 ET, market open
OPEN = dt.datetime(2026, 11, 18, 14, 0, tzinfo=UTC)
# Saturday noon ET, market closed
CLOSED = dt.datetime(2026, 11, 21, 17, 0, tzinfo=UTC)


@pytest.fixture
def env(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": "us-east-2", "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x",
                 "TABLE": "wt", "BUCKET": "ledger", "INGEST_KEY_PARAM": "/wt/key", "TELEGRAM_TOKEN_PARAM": "",
                 "HEARTBEAT_TIMEOUT_MIN": "5", "STALE_MIN": "3", "DAILY_LOSS_ALERT": "400"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.client("dynamodb")
        ddb.create_table(TableName="wt", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
        boto3.client("s3").create_bucket(Bucket="ledger", CreateBucketConfiguration={"LocationConstraint": "us-east-2"})
        boto3.client("ssm").put_parameter(Name="/wt/key", Value=KEY, Type="SecureString")
        sns = boto3.client("sns")
        topic = sns.create_topic(Name="alerts")["TopicArn"]
        monkeypatch.setenv("TOPIC_ARN", topic)
        for m in ("core", "api", "watchdog", "report"):
            sys.modules.pop(m, None)
        mods = {m: importlib.import_module(m) for m in ("core", "api", "watchdog", "report")}
        sent = []
        real_notify = mods["core"].notify
        monkeypatch.setattr(mods["core"], "notify", lambda s, l, **kw: (sent.append(s), real_notify(s, l, **kw)))
        mods["sent"] = sent
        yield mods


def at(env, monkeypatch, t):
    monkeypatch.setattr(env["core"], "now", lambda: t)


def call(env, route, body=None, key=KEY):
    ev = {"routeKey": route, "headers": {"x-atlas-key": key} if key else {}, "body": json.dumps(body) if body is not None else None}
    r = env["api"].handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def trade(i, pnl, closed):
    return {"bot": "atlas", "id": f"T-{i}", "symbol": "MNQZ6", "side": "long", "qty": 1, "pnl": pnl,
            "entry": 21000, "exit": 21000 + pnl / 2, "closed_at": closed.isoformat()}


# ---------------- market clock ----------------

def test_market_hours(env):
    c = env["core"]
    et = c.ET
    assert c.market_open(dt.datetime(2026, 11, 18, 9, 0, tzinfo=et))        # Wed morning
    assert not c.market_open(dt.datetime(2026, 11, 18, 17, 30, tzinfo=et))  # daily halt
    assert c.market_open(dt.datetime(2026, 11, 18, 18, 5, tzinfo=et))       # reopened
    assert not c.market_open(dt.datetime(2026, 11, 20, 17, 0, tzinfo=et))   # Friday close
    assert not c.market_open(dt.datetime(2026, 11, 21, 12, 0, tzinfo=et))   # Saturday
    assert not c.market_open(dt.datetime(2026, 11, 22, 17, 59, tzinfo=et))  # Sunday before open
    assert c.market_open(dt.datetime(2026, 11, 22, 18, 0, tzinfo=et))       # Sunday open


def test_trading_day_rolls_at_6pm(env):
    c = env["core"]
    assert c.trading_day(dt.datetime(2026, 11, 22, 19, 0, tzinfo=c.ET)) == "2026-11-23"  # Sunday night = Monday
    assert c.trading_day(dt.datetime(2026, 11, 18, 16, 59, tzinfo=c.ET)) == "2026-11-18"


def test_timestamps_accept_iso_and_epoch(env):
    c = env["core"]
    a = c.parse_ts("2026-11-18T14:00:00Z")
    assert a == c.parse_ts(a.timestamp()) == c.parse_ts(a.timestamp() * 1000) == OPEN


# ---------------- api ----------------

def test_rejects_missing_and_wrong_key(env):
    assert call(env, "GET /status", key=None)[0] == 401
    assert call(env, "GET /status", key="nope")[0] == 401


def test_heartbeat_then_status(env, monkeypatch):
    at(env, monkeypatch, OPEN)
    code, _ = call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": OPEN.isoformat(), "equity": 50210.5,
                                            "day_pnl": 210.5, "open_positions": 1, "version": "1.4.2"})
    assert code == 200
    code, body = call(env, "GET /status")
    bot = body["bots"][0]
    assert code == 200 and body["market_open"] and bot["health"] == "ok"
    assert bot["equity"] == 50210.5 and bot["open_positions"] == 1 and bot["version"] == "1.4.2"


def test_bad_payloads_are_400(env, monkeypatch):
    at(env, monkeypatch, OPEN)
    assert call(env, "POST /heartbeat", {"bot": "Bad Name!"})[0] == 400
    assert call(env, "POST /heartbeat", {"bot": "atlas", "equity": "lots"})[0] == 400
    assert call(env, "POST /trade", {"bot": "atlas", "id": "T-1"})[0] == 400
    assert call(env, "POST /trade", {**trade(1, 50, OPEN), "side": "sideways"})[0] == 400


def test_trade_lands_in_table_and_ledger_and_is_idempotent(env, monkeypatch):
    at(env, monkeypatch, OPEN)
    assert call(env, "POST /trade", trade(1, 50.0, OPEN)) == (201, {"ok": True, "trading_day": "2026-11-18"})
    assert call(env, "POST /trade", trade(1, 50.0, OPEN))[1].get("duplicate") is True
    obj = boto3.client("s3").get_object(Bucket="ledger", Key="trades/bot=atlas/day=2026-11-18/T-1.json")
    assert json.loads(obj["Body"].read())["pnl"] == 50.0
    items = boto3.resource("dynamodb").Table("wt").scan()["Items"]
    assert len([i for i in items if i["pk"].startswith("TRADES#")]) == 1


# ---------------- watchdog ----------------

def beat(env, monkeypatch, t, **extra):
    at(env, monkeypatch, t)
    call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": t.isoformat(), **extra})


def test_down_alert_fires_once_then_recovers(env, monkeypatch):
    beat(env, monkeypatch, OPEN)
    at(env, monkeypatch, OPEN + dt.timedelta(minutes=4))
    assert env["watchdog"].handler({}, None) == {"atlas": []}
    at(env, monkeypatch, OPEN + dt.timedelta(minutes=6))
    assert env["watchdog"].handler({}, None) == {"atlas": ["down"]}
    at(env, monkeypatch, OPEN + dt.timedelta(minutes=7))
    assert env["watchdog"].handler({}, None) == {"atlas": []}  # no spam
    beat(env, monkeypatch, OPEN + dt.timedelta(minutes=8))
    assert env["watchdog"].handler({}, None) == {"atlas": ["recovered"]}
    assert env["sent"] == ["🔴 ATLAS DOWN", "🟢 ATLAS back"]


def test_silence_on_weekend_is_not_an_alarm(env, monkeypatch):
    beat(env, monkeypatch, CLOSED - dt.timedelta(hours=20))
    at(env, monkeypatch, CLOSED)
    assert env["watchdog"].handler({}, None) == {"atlas": []}
    assert call(env, "GET /status")[1]["bots"][0]["health"] == "market closed"


def test_stale_data_alert(env, monkeypatch):
    at(env, monkeypatch, OPEN)
    call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": (OPEN - dt.timedelta(minutes=10)).isoformat()})
    assert env["watchdog"].handler({}, None) == {"atlas": ["stale"]}
    beat(env, monkeypatch, OPEN + dt.timedelta(minutes=1))
    assert env["watchdog"].handler({}, None) == {"atlas": ["fresh"]}


def test_loss_alert_once_per_day(env, monkeypatch):
    beat(env, monkeypatch, OPEN, day_pnl=-450)
    assert env["watchdog"].handler({}, None) == {"atlas": ["loss"]}
    beat(env, monkeypatch, OPEN + dt.timedelta(minutes=1), day_pnl=-500)
    assert env["watchdog"].handler({}, None) == {"atlas": []}
    beat(env, monkeypatch, OPEN + dt.timedelta(days=1), day_pnl=-420)  # next day: alert again
    assert env["watchdog"].handler({}, None) == {"atlas": ["loss"]}


# ---------------- daily report ----------------

def test_daily_report_math(env, monkeypatch):
    for i, pnl in enumerate([100, -40, -80, 60, 25]):
        at(env, monkeypatch, OPEN + dt.timedelta(minutes=i))
        call(env, "POST /trade", trade(i, pnl, OPEN + dt.timedelta(minutes=i)))
    beat(env, monkeypatch, OPEN + dt.timedelta(minutes=10), equity=50065)
    at(env, monkeypatch, dt.datetime(2026, 11, 18, 22, 10, tzinfo=UTC))  # 17:10 ET
    s = env["report"].handler({}, None)["atlas"]
    assert s["trades"] == 5 and s["wins"] == 3 and s["losses"] == 2 and s["win_rate"] == 60.0
    assert s["net"] == 65 and s["best"] == 100 and s["worst"] == -80
    assert s["max_drawdown"] == -120  # peak +100, trough -20
    assert env["sent"][-1] == "📊 ATLAS daily report +65.00"
    assert boto3.client("s3").get_object(Bucket="ledger", Key="reports/bot=atlas/day=2026-11-18.json")


def test_client_never_raises(env):
    sys.path.insert(0, str(ROOT / "client"))
    from atlas_watchtower import Watchtower
    wt = Watchtower(url="http://127.0.0.1:9", key="k")  # nothing listens on port 9
    assert wt._post("/heartbeat", {"bot": "atlas"}) is False
    wt.update(equity=1, last_tick=OPEN)
    wt.trade(id="T-1", symbol="X", side="long", qty=1, pnl=1)
    assert wt._state["last_tick"] == OPEN.isoformat() and wt._trades.qsize() == 1


# ---------------- dashboard ----------------

def test_dashboard_page_is_public_but_feed_is_not(env, monkeypatch):
    page = (ROOT / "src" / "index.html")
    if not page.exists():
        pytest.skip("no page yet")
    r = env["api"].handler({"routeKey": "GET /", "headers": {}}, None)
    assert r["statusCode"] == 200 and "text/html" in r["headers"]["content-type"]
    assert "frame-ancestors 'none'" in r["headers"]["content-security-policy"]
    assert call(env, "GET /feed", key=None)[0] == 401


def test_feed_has_everything_the_dashboard_draws(env, monkeypatch):
    for i, pnl in enumerate([100, -40, 25]):
        t = OPEN + dt.timedelta(minutes=i)
        at(env, monkeypatch, t)
        call(env, "POST /trade", trade(i, pnl, t))
        call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": t.isoformat(), "equity": 50000 + pnl, "day_pnl": pnl})
    beat(env, monkeypatch, OPEN + dt.timedelta(minutes=10), day_pnl=-450)
    env["watchdog"].handler({}, None)  # fires the loss alert, which lands in the alert log
    code, f = call(env, "GET /feed")
    assert code == 200 and f["bot"] == "atlas" and f["bots"] == ["atlas"] and f["trading_day"] == "2026-11-18"
    assert len(f["trades"]) == 3 and f["summary"]["net"] == 85
    assert len(f["beats"]) == 4 and f["beats"][0]["tick_lag"] == 0 and "equity" in f["beats"][0]
    assert f["alerts"][0]["kind"] == "loss" and f["state"]["health"] == "warning"
    assert "_down" not in f["state"] and f["config"]["daily_loss_alert"] == 400
    assert {"max_drawdown", "profit_target", "account_start"} <= set(f["config"])   # the console's account panel
    bad = env["api"].handler({"routeKey": "GET /feed", "headers": {"x-atlas-key": KEY},
                              "queryStringParameters": {"day": "nope"}}, None)
    assert bad["statusCode"] == 400


def test_thinning_keeps_the_last_sample(env):
    rows = list(range(2000))
    out = env["api"].thin(rows, cap=100)
    assert len(out) == 100 and out[-1] == 1999 and out[0] == 0


# ---------------- live market data ----------------

def bar(t, o, h, l, c, v=10):
    return {"t": t.isoformat(), "o": o, "h": h, "l": l, "c": c, "v": v}


def test_market_data_flows_to_feed(env, monkeypatch):
    at(env, monkeypatch, OPEN)
    m1, m2 = OPEN - dt.timedelta(minutes=2), OPEN - dt.timedelta(minutes=1)
    code, body = call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": OPEN.isoformat(), "symbol": "mnqz6",
                                               "price": 21012.5, "bid": 21012.25, "ask": 21012.5,
                                               "bars": [bar(m1, 21000, 21010, 20995, 21008), bar(m2, 21008, 21015, 21004, 21012.5)]})
    assert code == 200 and body["bars"] == 2
    # the same minute re-sent (a retry) overwrites instead of duplicating
    call(env, "POST /heartbeat", {"bot": "atlas", "symbol": "MNQZ6", "bars": [bar(m2, 21008, 21016, 21004, 21013)]})
    f = call(env, "GET /feed")[1]["market"]
    assert f["symbol"] == "MNQZ6" and f["quote"]["price"] == 21012.5 and f["quote"]["bid"] == 21012.25
    assert [b["c"] for b in f["bars"]] == [21008, 21013] and f["bars"][1]["h"] == 21016


def test_bad_market_data_is_rejected(env, monkeypatch):
    at(env, monkeypatch, OPEN)
    assert call(env, "POST /heartbeat", {"bot": "atlas", "price": 1})[0] == 400  # price without a symbol
    assert call(env, "POST /heartbeat", {"bot": "atlas", "symbol": "MNQ Z6!"})[0] == 400
    assert call(env, "POST /heartbeat", {"bot": "atlas", "symbol": "MNQZ6", "bars": [bar(OPEN, 10, 9, 8, 11)]})[0] == 400
    assert call(env, "POST /heartbeat", {"bot": "atlas", "symbol": "MNQZ6", "bars": [bar(OPEN, 1, 2, 0, 1)] * 11})[0] == 400


def test_client_builds_minute_bars_from_ticks(env):
    sys.path.insert(0, str(ROOT / "client"))
    from atlas_watchtower import Watchtower
    wt = Watchtower(url="http://127.0.0.1:9", key="k")
    base = 1_800_000_000 - (1_800_000_000 % 60)  # on a minute boundary
    for i, px in enumerate([100, 103, 99, 101]):
        wt.tick("MNQZ6", px, bid=px - .25, ask=px, at=base + i * 10)
    wt.tick("MNQZ6", 102, at=base + 61)  # next minute closes the first bar
    m = wt._market["MNQZ6"]
    b = m["bars"][0]
    assert (b["o"], b["h"], b["l"], b["c"]) == (100, 103, 99, 101) and len(m["bars"]) == 1
    assert m["quote"]["price"] == 102 and "price" not in wt._state  # prices never ride on the heartbeat
    wt.tick("MNQZ6", "not a number")  # must not raise into the trading loop


def test_client_forwards_finished_candles_and_skips_history(env):
    sys.path.insert(0, str(ROOT / "client"))
    from atlas_watchtower import Watchtower
    import time as _t
    wt = Watchtower(url="http://127.0.0.1:9", key="k")
    now = int(_t.time()) // 60 * 60
    wt.candle("MESZ6", now - 120, 6000, 6002, 5999, 6001, 50)
    wt.candle("MESZ6", dt.datetime.fromtimestamp(now - 60, UTC), 6001, 6003, 6000, 6002, 40)
    wt.candle("MESZ6", now - 400 * 86400, 1, 1, 1, 1)  # a year-old backfill candle: ignored
    wt.candle("MNQZ6", "garbage", 1, 1, 1, 1)           # bad input: ignored, never raised
    m = wt._market["MESZ6"]
    assert len(m["bars"]) == 2 and m["quote"]["price"] == 6002 and "MNQZ6" not in wt._market
    assert wt._state["last_tick"]
    wt._flush_market()  # nothing listens on port 9: bars must be kept for the next try
    assert len(wt._market["MESZ6"]["bars"]) == 2


def test_market_feed_never_counts_as_a_heartbeat(env, monkeypatch):
    beat(env, monkeypatch, OPEN)
    for i in range(1, 8):  # the price feed keeps streaming while the bot itself is silent
        at(env, monkeypatch, OPEN + dt.timedelta(minutes=i))
        code, _ = call(env, "POST /market", {"bot": "atlas", "symbol": "MNQ12-26", "price": 21000 + i, "bid": 20999.75 + i, "ask": 21000 + i,
                                            "bars": [bar(OPEN + dt.timedelta(minutes=i - 1), 21000, 21002, 20999, 21001)]})
        assert code == 200
    assert env["watchdog"].handler({}, None) == {"atlas": ["down"]}  # still caught
    f = call(env, "GET /feed")[1]
    assert f["market"]["symbol"] == "MNQ12-26" and f["market"]["quote"]["price"] == 21007 and len(f["market"]["bars"]) == 7
    assert call(env, "POST /market", {"bot": "atlas"})[0] == 400


def test_market_route_feeds_many_symbols_without_faking_a_heartbeat(env, monkeypatch):
    at(env, monkeypatch, OPEN)
    call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": OPEN.isoformat()})
    later = OPEN + dt.timedelta(minutes=8)
    at(env, monkeypatch, later)
    for sym, px in (("MESZ6", 6020.25), ("MNQZ6", 21010.5), ("MGCZ6", 2650.1)):
        code, b = call(env, "POST /market", {"bot": "atlas", "symbol": sym, "price": px,
                                             "bars": [bar(later - dt.timedelta(minutes=1), px, px + 1, px - 1, px)]})
        assert code == 200 and b["bars"] == 1
    f = call(env, "GET /feed")[1]
    assert f["market"]["symbols"] == ["MNQZ6", "MESZ6", "MGCZ6"]  # MNQ first
    assert f["market"]["symbol"] == "MNQZ6" and f["market"]["quote"]["price"] == 21010.5
    other = env["api"].handler({"routeKey": "GET /feed", "headers": {"x-atlas-key": KEY},
                                "queryStringParameters": {"symbol": "MESZ6"}}, None)
    assert json.loads(other["body"])["market"]["quote"]["price"] == 6020.25
    # prices kept flowing for 8 minutes, but the bot itself went quiet: the dead-man switch must still fire
    assert env["watchdog"].handler({}, None) == {"atlas": ["down"]}
    assert call(env, "POST /market", {"bot": "atlas", "symbol": "MNQZ6"})[0] == 400


# ---------------------------------------------------------------- the bot's own ants
def test_ant_health_rides_the_heartbeat_and_a_failing_ant_alerts_once(env, monkeypatch):
    at(env, monkeypatch, OPEN)
    ants = [{"name": "feed", "runs": 40, "failures": 0, "consecutive_failures": 0, "last_success": OPEN.isoformat()},
            {"name": "scout", "runs": 10, "failures": 3, "consecutive_failures": 3, "last_error": "KeyError('close')"}]
    assert call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": OPEN.isoformat(), "ants": ants, "paused": True,
                                         "drawdown": 120.5})[0] == 200
    st = call(env, "GET /feed")[1]["state"]
    assert [a["name"] for a in st["ants"]] == ["feed", "scout"] and st["paused"] is True and st["drawdown"] == 120.5
    assert env["watchdog"].handler({}, None) == {"atlas": ["ant:scout"]}
    assert env["watchdog"].handler({}, None) == {"atlas": []}            # once, not every minute
    ants[1]["consecutive_failures"] = 0
    call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": OPEN.isoformat(), "ants": ants})
    assert env["watchdog"].handler({}, None) == {"atlas": ["ant-ok:scout"]}
    assert call(env, "POST /heartbeat", {"bot": "atlas", "ants": [{"name": "bad name!"}]})[0] == 400


def test_drawdown_warning_fires_once_and_clears_with_margin(env, monkeypatch):
    monkeypatch.setenv("DRAWDOWN_ALERT", "750")
    beat(env, monkeypatch, OPEN, drawdown=500)
    call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": OPEN.isoformat(), "drawdown": 500})
    assert env["watchdog"].handler({}, None) == {"atlas": []}
    call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": OPEN.isoformat(), "drawdown": 760})
    assert env["watchdog"].handler({}, None) == {"atlas": ["drawdown"]}
    assert env["watchdog"].handler({}, None) == {"atlas": []}
    call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": OPEN.isoformat(), "drawdown": 700})   # inside the margin
    assert env["watchdog"].handler({}, None) == {"atlas": []}
    call(env, "POST /heartbeat", {"bot": "atlas", "last_tick": OPEN.isoformat(), "drawdown": 600})
    assert env["watchdog"].handler({}, None) == {"atlas": ["drawdown-ok"]}


def test_paper_tab_borrows_the_family_market(env, monkeypatch):
    at(env, monkeypatch, OPEN)
    call(env, "POST /market", {"bot": "atlas", "symbol": "MNQ", "price": 27302.5,
                               "bars": [bar(OPEN - dt.timedelta(minutes=1), 27300, 27305, 27299, 27302.5)]})
    call(env, "POST /heartbeat", {"bot": "atlas-paper", "last_tick": OPEN.isoformat()})
    r = env["api"].handler({"routeKey": "GET /feed", "headers": {"x-atlas-key": KEY},
                            "queryStringParameters": {"bot": "atlas-paper"}}, None)
    m = json.loads(r["body"])["market"]
    assert m["symbol"] == "MNQ" and m["source"] == "atlas" and len(m["bars"]) == 1 and m["quote"]["price"] == 27302.5
