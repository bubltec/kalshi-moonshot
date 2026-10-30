"""Daily review of the two moonshot lanes: would other settings have done better on what the tape recorded? Read-only. Changes nothing, places nothing.

For each family (coins for crypto_moonshot, energy for energy_moonshot) every second of every settled market's last 120 s is checked for a cheap side (ask 2-HI) that takers bought at least BAR dollars of in 3 s (BTC double the
bar for coins). Each setting in the grid below picks, per market, the FIRST second that passes its gates and "buys" that side at that ask (taker fee included, held to settlement); the table gives the events, win rate, edge
per ticket and ROI, and the edge in the older and the newer half of the days. A row is flagged only when it has at least MIN_EVENTS events and is positive in BOTH halves. The row marked "lane now" is the lane's own
settings, and the paper bets the lane actually made in the same days are printed under it, so a gap between the replay and the lane shows up as a gap in the counts.

    scripts/cloud.sh run scripts/moonshot_review.py [DAYS=7]       (the cloud tape: the longest history)
    docker compose exec -T -e DAYS=1 bot python - < scripts/moonshot_review.py       (the Mac tape: what the Mac's paper lanes saw)
"""
import itertools
import os
import re
import statistics
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import enabled_lanes
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "7"))
MIN_EVENTS = 15
LO = 0.02
WINDOW, MIN_LEFT = 120, 10
FAMILIES = {"coins": ("crypto_moonshot", {"BTC", "ETH", "SOL", "XRP", "DOGE"}), "energy": ("energy_moonshot", {"WTI", "BRENT", "NATGAS"})}
GRID = {"bar": (200.0, 400.0, 800.0), "spike": (0.0, 3.0, 5.0), "crash_park": (False, True), "hi": (0.12, 0.20)}
CRASH, CRASH_SECONDS, PARK_SECONDS, PARK_SPAN, PARK_HIGH = 0.10, 12, 20, 0.06, 0.10


def name_of(series):
    m = re.match(r"^KX([A-Z]+)15M$", series)
    return m.group(1) if m else ""


def lane_settings(lane):
    for spec in enabled_lanes(s):
        if spec.name == lane:
            return {"bar": float(spec.hunt_dollars), "spike": float(spec.hunt_spike), "crash_park": spec.hunt_crash < 1.0 or spec.hunt_park_span < 1.0, "hi": float(spec.price[1])}
    return None


since = time.time() - DAYS * 86400
wrows = main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall()
print(f"moonshot review, {DAYS:g} days of the tape at {s.tape_path}\n")
for fam, (lane, names) in FAMILIES.items():
    cand = {}  # ticker -> (close, result, [(t, side, ask, d3, ratio, crashed, park_ok_at_hi)])
    markets = 0
    for w in wrows:
        if name_of(w["series"]) not in names:
            continue
        close = float(w["close_ts"])
        quotes = [(float(t), float(b), float(a)) for t, b, a in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 260, close)).fetchall()]
        flow = {}
        for t, yv, nv, vw in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], int(close - 260), int(close))).fetchall():
            d = flow.setdefault(int(t), [0.0, 0.0])
            vw = 0.5 if vw is None else float(vw)
            d[0] += float(yv or 0) * vw
            d[1] += float(nv or 0) * (1.0 - vw)
        if len(quotes) < 10 or not flow:
            continue
        markets += 1
        rows = []
        for t in range(int(close - WINDOW), int(close - MIN_LEFT)):
            q = [x for x in quotes if x[0] <= t and t - x[0] <= 6]
            if not q:
                continue
            yb, ya = q[-1][1], q[-1][2]
            for side, ask in (("yes", ya), ("no", 1.0 - yb)):
                if not LO <= ask <= 0.20:
                    continue
                idx = 0 if side == "yes" else 1
                d3 = sum(flow.get(t - k, [0.0, 0.0])[idx] for k in range(3))
                if d3 < 200.0:
                    continue
                normal3 = 3.0 * statistics.median([flow.get(t - 3 - k, [0.0, 0.0])[idx] for k in range(120)])
                hist = [(x[0], x[2] if side == "yes" else 1.0 - x[1]) for x in quotes if t - 40 <= x[0] <= t]
                crashed = any(ask <= a - CRASH for ts_, a in hist if t - CRASH_SECONDS <= ts_ <= t)
                recent = [a for ts_, a in hist if t - PARK_SECONDS <= ts_ <= t]
                rows.append((t, side, ask, d3, d3 / max(normal3, 1.0), crashed, (len(recent) >= 3, max(recent) if recent else 1.0)))
        if rows:
            cand[w["ticker"]] = (close, w["result"], name_of(w["series"]), rows)
    closes = sorted(c[0] for c in cand.values())
    mid = closes[len(closes) // 2] if closes else 0
    print(f"== {fam}: {markets} markets with a tape, {len(cand)} with a $200 cheap-side burst ({lane})")
    print(f"  {'bar':>5} {'spike':>5} {'crash+park':>10} {'top':>5} | {'events':>6} {'wins':>5} {'win%':>6} {'ask':>6} {'edge/ticket':>11} {'ROI':>6} | {'old':>8} {'new':>8}")
    now_cfg = lane_settings(lane)
    out = []
    for bar, spike, cp, hi in itertools.product(*GRID.values()):
        picks = []
        for close, result, nm, rows in cand.values():
            b = bar * (2.0 if nm == "BTC" else 1.0)
            for t, side, ask, d3, ratio, crashed, (enough, rmax) in rows:
                if ask > hi or d3 < b or (spike > 0 and ratio < spike):
                    continue
                if cp and (crashed or not enough or rmax > max(hi, PARK_HIGH) or rmax > ask + PARK_SPAN):
                    continue
                picks.append((close, ask, result == side))
                break
        if not picks:
            continue
        def edge(g):
            return sum((100.0 if won else 0.0) - a * 100 - fee_per_contract(a) * 100 for _c, a, won in g) / len(g)
        n = len(picks)
        wins = sum(1 for p in picks if p[2])
        ask = sum(p[1] for p in picks) / n * 100
        e = edge(picks)
        old, new = [p for p in picks if p[0] < mid], [p for p in picks if p[0] >= mid]
        oe = f"{edge(old):>+7.1f}c" if len(old) >= 4 else "       -"
        ne = f"{edge(new):>+7.1f}c" if len(new) >= 4 else "       -"
        good = n >= MIN_EVENTS and len(old) >= 4 and len(new) >= 4 and e > 0 and edge(old) > 0 and edge(new) > 0
        mark = "  <== now" if now_cfg == {"bar": bar, "spike": spike, "crash_park": cp, "hi": hi} else ""
        out.append((e, f"  {bar:>5.0f} {spike:>5.0f} {'on' if cp else 'off':>10} {hi:>5.2f} | {n:>6} {wins:>5} {wins / n:>6.1%} {ask:>5.1f}c {e:>+10.2f}c {e / ask:>+6.0%} | {oe} {ne}{'  *** both halves' if good else ''}{mark}"))
    for _e, text in sorted(out, reverse=True):
        print(text)
    if now_cfg is not None and not any("<== now" in t for _e, t in out):
        print(f"  (the lane's own settings {now_cfg} are outside the grid or had no events)")
    bets = main.db.execute("SELECT d.mode, d.side, d.ask, w.result FROM decisions d LEFT JOIN windows w ON w.ticker = d.ticker WHERE d.lane = ? AND d.action = 'bet' AND d.ts > ?", (lane, since)).fetchall()
    done = [b for b in bets if b["result"] in ("yes", "no")]
    if done:
        pnl = sum((100.0 if b["side"] == b["result"] else 0.0) - float(b["ask"]) * 100 - fee_per_contract(float(b["ask"])) * 100 for b in done) / len(done)
        print(f"  {lane} paper/live bets in the same days: {len(bets)} placed, {len(done)} settled, {sum(1 for b in done if b['side'] == b['result'])} won, {pnl:+.2f}c per ticket after fees")
    else:
        print(f"  {lane}: {len(bets)} bets in the same days, none settled yet")
    print()
print(f"A row marked *** is positive in both halves with at least {MIN_EVENTS} events. Replay events are not bets: a live buy also needs a fill, and the replay is per market, first second only.")
