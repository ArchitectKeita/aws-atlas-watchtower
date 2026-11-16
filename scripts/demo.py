"""Live demo: pretend to be Atlas for a few minutes so you can watch Watchtower react.

    $env:WATCHTOWER_URL = terraform output -raw watchtower_url
    $env:WATCHTOWER_KEY = <run the key_command output>
    python scripts/demo.py

What you'll see (during market hours):
  1. heartbeats + a handful of fake trades land          -> GET /status shows "ok"
  2. it sends a heartbeat whose price tick is 10 min old -> 🟡 STALE DATA alert within a minute
  3. it goes silent                                      -> 🔴 DOWN alert after ~5 minutes
  4. run with --report to fire the daily report on demand (needs AWS CLI access)
  5. run with --forget when you're done, so "demo" stops appearing in daily reports

Uses bot name "demo" so it never mixes with Atlas's real numbers.
"""
import datetime as dt
import json
import os
import random
import sys
import time
import urllib.request

URL = os.environ["WATCHTOWER_URL"].rstrip("/")
KEY = os.environ["WATCHTOWER_KEY"]
BOT = "demo"


def call(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"content-type": "application/json", "x-atlas-key": KEY})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def now():
    return dt.datetime.now(dt.timezone.utc)


def main():
    if "--report" in sys.argv:
        os.system('aws lambda invoke --region us-east-2 --function-name atlas-watchtower-report --cli-binary-format raw-in-base64-out '
                  '--payload "{}" report.json && type report.json')
        return

    if "--forget" in sys.argv:
        os.system('aws dynamodb delete-item --region us-east-2 --table-name atlas-watchtower '
                  '--key "{\\"pk\\":{\\"S\\":\\"STATES\\"},\\"sk\\":{\\"S\\":\\"demo\\"}}"')
        print("demo bot removed")
        return

    print("1) Acting like a healthy bot: 90 minutes of MNQ price action, heartbeats + trades")
    # A fake 1-minute price history ending now, sent the way the real client sends it: 10 bars per heartbeat.
    end = now().replace(second=0, microsecond=0)
    bars, px = [], 21000.0
    for k in range(90, 0, -1):
        o = px
        hi = lo = c = o
        for _ in range(6):
            c += random.uniform(-3.2, 3.4)
            hi, lo = max(hi, c), min(lo, c)
        q = lambda v: round(v * 4) / 4
        bars.append({"t": (end - dt.timedelta(minutes=k)).isoformat(), "o": q(o), "h": q(hi + random.random()), "l": q(lo - random.random()), "c": q(c), "v": random.randint(80, 900)})
        px = c
    equity, day_pnl = 50000.0, 0.0
    run = int(time.time())
    call("POST", "/heartbeat", {"bot": BOT, "last_tick": now().isoformat(), "equity": equity, "day_pnl": 0,
                                "open_positions": 0, "version": "demo"})
    for chunk in range(0, len(bars), 30):  # prices go to /market, exactly like the real client
        part = bars[chunk:chunk + 30]
        call("POST", "/market", {"bot": BOT, "symbol": "MNQZ6", "price": part[-1]["c"], "bid": part[-1]["c"] - .25,
                                 "ask": part[-1]["c"], "bars": part})
    for i, (a, b) in enumerate([(8, 19), (24, 31), (37, 52), (58, 63), (69, 80), (83, 88)]):
        long = random.random() < .55
        entry, exit_ = bars[a]["c"], bars[b]["c"]
        pnl = round((exit_ - entry if long else entry - exit_) * 2, 2)
        day_pnl += pnl
        equity += pnl
        call("POST", "/trade", {"bot": BOT, "id": f"D{run}-{i}", "symbol": "MNQZ6", "side": "long" if long else "short", "qty": 1,
                                "entry": entry, "exit": exit_, "pnl": pnl, "opened_at": bars[a]["t"], "closed_at": bars[b]["t"]})
        print(f"   trade {i + 1}: {'LONG ' if long else 'SHORT'} {entry:,.2f} -> {exit_:,.2f}  {pnl:+.2f}   day {day_pnl:+.2f}")
    call("POST", "/heartbeat", {"bot": BOT, "last_tick": now().isoformat(),
                                "equity": round(equity, 2), "day_pnl": round(day_pnl, 2), "open_positions": 0, "version": "demo"})
    print("   -> open the console and pick DEMO: candles, entries/exits, P&L")

    status = call("GET", "/status")
    demo = next(b for b in status["bots"] if b["bot"] == BOT)
    print(f"   status -> health={demo['health']}  equity={demo['equity']}  market_open={status['market_open']}")

    print("\n2) Frozen price feed: bot alive, last tick 10 minutes old")
    call("POST", "/heartbeat", {"bot": BOT, "last_tick": (now() - dt.timedelta(minutes=10)).isoformat()})
    print("   -> watch for 🟡 DEMO stale data within ~1 minute")

    print("\n3) Going silent now. Leave this window alone.")
    print("   -> 🔴 DEMO DOWN arrives about 5 minutes from now (market hours only)")
    print("   -> re-run this script afterwards to see 🟢 DEMO back")


if __name__ == "__main__":
    main()
