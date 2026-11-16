"""Drop-in Watchtower client for Atlas. Standard library only.

    from atlas_watchtower import Watchtower
    wt = Watchtower(url=os.environ["WATCHTOWER_URL"], key=os.environ["WATCHTOWER_KEY"], bot="atlas")
    wt.start()                                   # background heartbeat every 30s
    wt.update(last_tick=tick_time, equity=eq, day_pnl=pnl, open_positions=n)   # whenever you have fresh numbers
    wt.candle("MNQZ6", t, o, h, l, c, v)        # optional: each finished 1-min candle (what AtlasFeed sends) -> live chart
    wt.tick("MNQZ6", price, bid=bid, ask=ask)   #   ...or raw ticks, and the client builds the candles itself
    wt.trade(id="T-1042", symbol="MNQZ6", side="long", qty=2, entry=..., exit=..., pnl=50.0,
             opened_at=..., closed_at=...)       # after each closed trade

Rules this client keeps so it can never hurt the trading loop:
  * nothing here blocks the caller - all network calls happen on a background thread
  * every call has a 3 second timeout and every error is swallowed (logged, never raised)
  * failed trade reports are retried; the server ignores duplicates, so retries are safe
"""
import json
import logging
import queue
import threading
import time
import urllib.request

log = logging.getLogger("watchtower")


class Watchtower:
    def __init__(self, url, key, bot="atlas", interval=30, version=None):
        self.url, self.key, self.bot, self.interval = url.rstrip("/"), key, bot, interval
        self.version = version
        self._state = {}
        self._trades = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bar = None          # the 1-minute bar being built from ticks
        self._market = {}         # symbol -> {"bars": [...], "quote": {...}} waiting to be sent
        self._next_market = 0.0

    # ---------------- public ----------------
    def start(self):
        threading.Thread(target=self._loop, name="watchtower", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def update(self, **fields):
        """last_tick (datetime/ISO/epoch), equity, day_pnl, open_positions."""
        with self._lock:
            self._state.update({k: _jsonable(v) for k, v in fields.items() if v is not None})

    def tick(self, symbol, price, bid=None, ask=None, size=0, at=None):
        """Feed every price update here. Cheap: no network, just arithmetic under a lock.
        Builds 1-minute OHLC bars and ships the closed ones with the next heartbeat."""
        try:
            ts = at.timestamp() if hasattr(at, "timestamp") else (float(at) if at else time.time())
            minute = int(ts // 60) * 60
            with self._lock:
                b = self._bar
                if b is None or b["sym"] != symbol or b["m"] != minute:
                    if b is not None:
                        self._queue_bar(b["sym"], b["m"], b["o"], b["h"], b["l"], b["c"], b["v"])
                    b = self._bar = {"sym": symbol, "m": minute, "o": price, "h": price, "l": price, "c": price, "v": 0}
                b["h"] = max(b["h"], price)
                b["l"] = min(b["l"], price)
                b["c"] = price
                b["v"] += size or 0
                self._state["last_tick"] = _iso(ts)
                q = self._market.setdefault(symbol, {"bars": [], "quote": {}})["quote"]
                q["price"] = price
                if bid is not None:
                    q["bid"] = bid
                if ask is not None:
                    q["ask"] = ask
        except Exception as e:  # a bad tick must never reach the trading loop
            log.warning("watchtower tick ignored: %s", e)

    def candle(self, symbol, t, o, h, l, c, v=0):
        """A finished 1-minute candle, e.g. straight from AtlasFeed. Also counts as fresh market data for the
        stale-feed alert, and its close becomes the symbol's last price."""
        try:
            ts = t.timestamp() if hasattr(t, "timestamp") else (_parse_ts(t) if isinstance(t, str) else float(t))
            if ts < time.time() - 86400:
                return  # history backfills stay between AtlasFeed and Atlas; the console only needs today
            with self._lock:
                self._queue_bar(symbol, int(ts // 60) * 60, o, h, l, c, v)
                self._market[symbol]["quote"]["price"] = c
                fresh = min(ts + 60, time.time())  # a candle is "seen" when it closes
                prev = self._state.get("last_tick")
                if prev is None or fresh > _parse_ts(prev):
                    self._state["last_tick"] = _iso(fresh)
        except Exception as e:
            log.warning("watchtower candle ignored: %s", e)

    def _queue_bar(self, symbol, minute, o, h, l, c, v):  # caller holds the lock
        bars = self._market.setdefault(symbol, {"bars": [], "quote": {}})["bars"]
        bars.append({"t": _iso(minute), "o": o, "h": h, "l": l, "c": c, "v": v})
        del bars[:-120]  # a long outage can't pile up memory

    def trade(self, **t):
        try:
            self._trades.put_nowait({k: _jsonable(v) for k, v in t.items()})
        except queue.Full:
            log.warning("watchtower trade queue full; dropping %s", t.get("id"))

    # ---------------- internals ----------------
    def _loop(self):
        next_beat = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= next_beat:
                with self._lock:
                    body = {"bot": self.bot, **self._state}
                if self.version:
                    body["version"] = self.version
                self._post("/heartbeat", body)
                next_beat = time.monotonic() + self.interval
            if time.monotonic() >= self._next_market:
                self._flush_market()
                self._next_market = time.monotonic() + 5
            try:
                t = self._trades.get(timeout=1)
            except queue.Empty:
                continue
            if not self._post("/trade", {"bot": self.bot, **t}):
                time.sleep(2)
                try:
                    self._trades.put_nowait(t)  # try again later
                except queue.Full:
                    pass

    def _flush_market(self):
        """Prices go to /market, never with the heartbeat, so a live feed can't make a dead bot look alive."""
        with self._lock:
            pending, self._market = self._market, {}
        for sym, m in pending.items():
            bars, quote = m["bars"], m["quote"]
            while bars or quote:
                chunk, bars = bars[:60], bars[60:]
                ok = self._post("/market", {"bot": self.bot, "symbol": sym, **quote, "bars": chunk})
                quote = {}
                if not ok:  # put unsent bars back for the next flush
                    with self._lock:
                        self._market.setdefault(sym, {"bars": [], "quote": {}})["bars"][:0] = chunk + bars
                    break

    def _post(self, path, body):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "x-atlas-key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            log.warning("watchtower %s -> %s %s", path, e.code, e.read()[:200])
            return e.code == 400  # a bad payload won't fix itself; don't retry it forever
        except Exception as e:
            log.warning("watchtower %s failed: %s", path, e)
            return False


def _parse_ts(s):
    import datetime as _dt
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _iso(ts):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat()


def _jsonable(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v
