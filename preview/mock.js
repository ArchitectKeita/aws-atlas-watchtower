// Preview-only fake backend: a believable session so the dashboard can be seen without AWS.
(() => {
  const rnd = (() => { let s = 7; return () => (s = (s * 16807) % 2147483647) / 2147483647; })();
  const now = Date.now(), iso = (t) => new Date(t).toISOString();
  const start = now - 4 * 3600e3, gapA = now - 150 * 60e3, gapB = gapA + 7 * 60e3; // one 7-minute flatline
  // 1-minute MNQ bars: a random walk with a little trend and intraday swings
  const bars = [], m0 = Math.floor(start / 60e3) * 60e3;
  let px = 21018;
  for (let m = m0; m < now - 60e3; m += 60e3) {
    const o = px, drift = Math.sin(m / 2.4e6) * .9 + .08, vol = 4 + rnd() * 6;
    let h = o, l = o, c = o;
    for (let k2 = 0; k2 < 6; k2++) { c += (rnd() - .5) * vol + drift / 6; h = Math.max(h, c); l = Math.min(l, c); }
    const q = (v) => Math.round(v * 4) / 4;
    bars.push({ t: iso(m), o: q(o), h: q(h + rnd() * 2), l: q(l - rnd() * 2), c: q(c), v: Math.round(80 + rnd() * 900) });
    px = c;
  }
  const barAt = (t) => bars[Math.max(0, Math.min(bars.length - 1, Math.floor((t - m0) / 60e3)))];
  const trades = [];
  let run = 0, t = start + 12 * 60e3, i = 1040;
  while (t < now - 6 * 60e3) {
    const hold = (3 + rnd() * 14) * 60e3, open = t - hold, long = rnd() < .55, qty = rnd() < .3 ? 2 : 1;
    const entry = barAt(open).c, exit = barAt(t).c;
    const pnl = Math.round((long ? exit - entry : entry - exit) * 2 * qty * 100) / 100;
    run += pnl;
    trades.push({ sk: iso(t), bot: "atlas", id: `T-${i++}`, symbol: "MNQZ6", side: long ? "long" : "short", qty,
      entry, exit, pnl, opened_at: iso(open), closed_at: iso(t), run });
    t += (10 + rnd() * 20) * 60e3;
  }
  const beats = [];
  let eq = 50000, k = 0;
  for (let b = start; b < now - 18e3; b += 30e3) {
    if (b > gapA && b < gapB) continue;
    while (k < trades.length && Date.parse(trades[k].closed_at) <= b) k++;
    const realized = k ? trades[k - 1].run : 0, open = Math.sin(b / 4e5) * 22 * (rnd() < .5 ? 1 : .4);
    const lag = b > gapB && b < gapB + 6 * 60e3 ? 180 + rnd() * 260 : rnd() * 3;
    beats.push({ t: iso(b), equity: Math.round((eq + realized + open) * 100) / 100, day_pnl: Math.round((realized + open) * 100) / 100,
      tick_lag: Math.round(lag * 10) / 10, open_positions: Math.abs(open) > 10 ? 1 : 0 });
  }
  const A = (t, kind, subject, body) => ({ sk: `${iso(t)}#atlas#${kind}`, bot: "atlas", kind, subject, body });
  const alerts = [
    A(gapB + 6.5 * 60e3, "fresh", "🟢 ATLAS data flowing", "Market data is fresh again."),
    A(gapB + 60e3, "stale", "🟡 ATLAS stale data", "Bot is alive but its last market tick was 4.1 min old when it checked in.\nThe data feed may be frozen - the bot could be trading on old prices.\nOpen positions: 1"),
    A(gapB + 30e3, "recovered", "🟢 ATLAS back", "Heartbeat restored (0.4 min ago)."),
    A(gapA + 5.5 * 60e3, "down", "🔴 ATLAS DOWN", "No heartbeat for 5.5 min while the market is open.\nOpen positions: 1\nCheck the bot host and the broker platform now."),
    A(now - 20 * 3600e3, "report", "📊 ATLAS daily report +412.50", "Net P&L: +412.50   (14 trades, 9W / 5L, 64.3% win rate)\nBest +138.00   Worst -76.25   Max drawdown -151.75"),
  ];
  const last = beats[beats.length - 1];
  const sum = (() => {
    const p = trades.map(x => x.pnl), w = p.filter(v => v > 0), l = p.filter(v => v < 0);
    let pk = 0, c = 0, dd = 0; for (const v of p) { c += v; pk = Math.max(pk, c); dd = Math.min(dd, c - pk); }
    const r2 = (v) => Math.round(v * 100) / 100;
    return { trades: p.length, wins: w.length, losses: l.length, win_rate: Math.round(1000 * w.length / p.length) / 10, net: r2(c),
      best: Math.max(...p), worst: Math.min(...p), avg_win: r2(w.reduce((a, b) => a + b, 0) / w.length),
      avg_loss: r2(l.reduce((a, b) => a + b, 0) / l.length), max_drawdown: r2(dd) };
  })();
  const fmt = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" });
  const today = fmt.format(new Date(now + 6 * 3600e3 * (new Date(now).getUTCHours() >= 23 ? 1 : 0)));
  window.WT_MOCK = async (path, S) => {
    await new Promise(r => setTimeout(r, 280));
    if (S.key === "wrong") return { status: 401 };
    const day = new URLSearchParams(path.split("?")[1] || "").get("day") || today;
    const isToday = day === today;
    const ageMin = (Date.now() - Date.parse(last.t)) / 60e3;
    return { status: 200, body: {
      now: new Date().toISOString(), market_open: true, trading_day: day, bots: ["atlas", "demo"], bot: S.bot || "atlas",
      config: { heartbeat_timeout_min: 5, stale_min: 3, daily_loss_alert: 400 },
      state: { bot: "atlas", health: "ok", heartbeat_age_min: Math.round(ageMin * 10) / 10, tick_age_min: .1, tick_lag_min: last.tick_lag / 60,
        day_pnl: last.day_pnl, equity: last.equity, open_positions: last.open_positions, version: "0.2.9", paused: false, drawdown: 182.5,
        ants: ["news", "sentinel", "feed", "scout", "broker", "shadow", "shadow_mes", "shadow_mgc", "shadow_mym", "whatif", "journal", "messenger", "reporter", "uplink", "dashboard"].map((n, i) => ({ name: n, runs: 400 - i * 20, failures: n === "messenger" ? 4 : 0, consecutive_failures: n === "messenger" ? 4 : 0, last_error: n === "messenger" ? "TimeoutError('telegram')" : null })) },
      summary: isToday ? sum : { trades: 0, wins: 0, losses: 0, win_rate: null, net: 0, best: null, worst: null, avg_win: null, avg_loss: null, max_drawdown: 0 },
      trades: isToday ? trades.map(({ run, ...x }) => x) : [], beats: isToday ? beats : [], alerts,
      market: (() => {
        const SY = { MNQZ6: 1, MESZ6: 6020 / 21018, MYMZ6: 44100 / 21018, MGCZ6: 2655 / 21018, SILZ6: 31.4 / 21018 };
        const sym = new URLSearchParams(path.split("?")[1] || "").get("symbol") || "MNQZ6", k = SY[sym] || 1;
        const r = (v) => Math.round(v * k * 100) / 100;
        const bs = isToday ? bars.map(b => ({ ...b, o: r(b.o), h: r(b.h), l: r(b.l), c: r(b.c) })) : [];
        const last = bs[bs.length - 1];
        return { symbol: sym, symbols: Object.keys(SY), bars: bs,
          quote: last ? { price: last.c, bid: r(bars[bars.length - 1].c - .25), ask: last.c, price_at: new Date(Date.now() - 2e3).toISOString() } : null };
      })(),
    } };
  };
})();
