"""The last 3 minutes of a crypto 15-minute market by trend, distance from the strike, and whether the index is AT ITS FARTHEST FROM THE STRIKE of the whole window so far. Read-only.

AT EXTREME: for a YES bet the index is at (within EXT_BPS of) its highest print since the window opened and above the strike; for NO, its lowest and below. The table splits each ZMIN into "at extreme" and "not at extreme" so the condition can be judged against the same rule without it. SCAN=1 also tries the first second in minutes 12-14 the index sets a new window extreme on the trend's side (entry 2 s later, z measured then).


At minute 12 (3 minutes left) of each BTC/ETH/SOL/XRP/DOGE window, from the settlement index ticks (the price Kalshi's "NOW" shows) and the window's strike ("TO BEAT"):
  trend    : the index over minutes 9-12 is up / down / mixed. up or down needs a net move of at least TREND_BPS basis points and a path efficiency (net / total movement of the 1 s ticks, smoothed to 5 s) of at least EFF; anything else is mixed.
  distance : z = ln(index / strike) / (sigma * sqrt(180)), the distance from the strike in units of how far the coin typically moves in the 3 minutes left (sigma = standard deviation of the 1 s log changes over minutes 6-12). Z is the confidence.
Rule: trend up and z >= ZMIN -> buy YES; trend down and z <= -ZMIN -> buy NO; otherwise pass. Bought at the ask 2 s after minute 12 with the taker fee, held to the result.
Printed: each ZMIN, trades, ask paid, win rate, result per contract; and how often the side won against what its ask said (calibration by z bucket, all windows, no trend rule) so the price can be compared with the outcome.

    docker compose -p moonshot-local --env-file .env.local exec -T -e DAYS=8 bot python - < scripts/late_distance_replay.py
"""
import bisect
import math
import os
import re
import statistics
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
DAYS = float(os.environ.get("DAYS", "8"))
TREND_BPS, EFF = float(os.environ.get("TREND_BPS", "3")), float(os.environ.get("EFF", "0.25"))
ZMINS = [float(x) for x in os.environ.get("ZMIN", "0,0.5,1.0,1.5,2.0").split(",")]
ENTRY = 2
STEPS = int(os.environ.get("STEPS", "6"))  # the vote samples the 3 minutes in this many equal steps (odd, so no split vote)
VOTES = int(os.environ.get("VOTES", "0"))  # 0 = the older bps/efficiency trend; n = the 30 s vote: of the 6 steps over minutes 9-12, at least n up (or n down) is the trend, else mixed
EXT_BPS = float(os.environ.get("EXT_BPS", "0.5"))
SCAN = os.environ.get("SCAN", "0") == "1"

wins = main.db.execute("SELECT ticker, series, close_ts, result, strike FROM windows WHERE result IN ('yes','no') AND strike IS NOT NULL AND close_ts > ? ORDER BY close_ts", (time.time() - DAYS * 86400,)).fetchall()
rows = []
for w in wins:
    idx = index_for_series(w["series"])
    if idx is None or not re.match(r"^KX(BTC|ETH|SOL|XRP|DOGE)15M$", w["series"]):
        continue
    close, strike = float(w["close_ts"]), float(w["strike"])
    t12 = close - 180
    ticks = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (idx, close - 900, close - 30)).fetchall()
    if len(ticks) < 300:
        continue
    ts = [float(r[0]) for r in ticks]
    val = [float(r[1]) for r in ticks]

    def at(t):
        i = bisect.bisect_right(ts, t) - 1
        return val[i] if i >= 0 and t - ts[i] <= 5 else None

    grid = [(t, at(t)) for t in range(int(close - 900), int(close - 30))]
    grid = [(t, v) for t, v in grid if v]
    if len(grid) < 400:
        continue
    px = {t: v for t, v in grid}
    hist = [px[t] for t in range(int(close - 360), int(t12) + 1, 5) if t in px]  # minutes 9-12, every 5 s
    if len(hist) < 30 or int(t12) not in px:
        continue
    net = (hist[-1] / hist[0] - 1.0) * 1e4
    total = sum(abs(math.log(b / a)) for a, b in zip(hist, hist[1:])) or 1e-12
    eff = abs(math.log(hist[-1] / hist[0])) / total
    trend = ("up" if net > 0 else "down") if abs(net) >= TREND_BPS and eff >= EFF else "mixed"
    if VOTES:
        pts30 = [px.get(int(close - 360 + 180 * k / STEPS)) for k in range(STEPS + 1)]
        if any(v is None for v in pts30):
            continue
        ups, downs = sum(1 for a, b in zip(pts30, pts30[1:]) if b > a), sum(1 for a, b in zip(pts30, pts30[1:]) if b < a)
        trend = "up" if ups >= VOTES and ups > downs else "down" if downs >= VOTES and downs > ups else "mixed"
    rets = [math.log(px[t] / px[t - 1]) for t in range(int(close - 360), int(t12) + 1) if t in px and t - 1 in px]
    sigma = statistics.pstdev(rets) if len(rets) > 100 else 0.0
    if sigma <= 0:
        continue
    z = math.log(px[int(t12)] / strike) / (sigma * math.sqrt(180))
    q = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? AND ts <= ? AND bid > 0 AND ask > 0 ORDER BY ts DESC LIMIT 1", (w["ticker"], t12 + ENTRY)).fetchone()
    if not q:
        continue
    run_hi = max(v for t, v in px.items() if t <= t12)
    run_lo = min(v for t, v in px.items() if t <= t12)
    cur = px[int(t12)]
    ext = "yes" if cur >= run_hi * (1 - EXT_BPS / 1e4) and cur > strike else "no" if cur <= run_lo * (1 + EXT_BPS / 1e4) and cur < strike else None
    scan = None
    if SCAN and trend != "mixed":
        hi, lo = max(v for t, v in px.items() if t <= close - 360 + 180), min(v for t, v in px.items() if t <= t12)
        hi, lo = run_hi, run_lo
        for t in range(int(t12) + 1, int(close - 60)):
            if t not in px:
                continue
            v = px[t]
            if trend == "up" and v > hi and v > strike:
                scan = (t, "yes", math.log(v / strike) / (sigma * math.sqrt(close - t)))
                break
            if trend == "down" and v < lo and v < strike:
                scan = (t, "no", math.log(v / strike) / (sigma * math.sqrt(close - t)))
                break
            hi, lo = max(hi, v), min(lo, v)
    scan_q = None
    if scan:
        scan_q = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? AND ts <= ? AND bid > 0 AND ask > 0 ORDER BY ts DESC LIMIT 1", (w["ticker"], scan[0] + ENTRY)).fetchone()
    rows.append({"ext": ext, "scan": scan if scan_q else None, "scan_q": scan_q, "close": close, "result": w["result"], "trend": trend, "z": z, "bid": float(q[0]), "ask": float(q[1]), "net": net, "eff": eff})
print(f"{len(rows)} windows with index ticks, strike and a quote; trend needs {TREND_BPS:g} bps and efficiency {EFF:g}")
print("trends:", {k: sum(1 for r in rows if r["trend"] == k) for k in ("up", "down", "mixed")}, "\n")


def result(r, side):
    ask = r["ask"] if side == "yes" else 1.0 - r["bid"]
    if not 0.01 < ask < 0.99:
        return None
    return (1.0 if r["result"] == side else 0.0) - ask - fee_per_contract(ask), ask


print(f"{'ZMIN':>5} {'extreme?':>9} | {'trades':>6} {'share':>5} | {'paid':>5} {'won':>4} | {'per contract':>13} | older / newer halves")
for zmin in ZMINS:
    for label, want in (("at extreme", True), ("not at it", False)):
        trades = []
        for r in sorted(rows, key=lambda r: r["close"]):
            side = "yes" if r["trend"] == "up" and r["z"] >= zmin else "no" if r["trend"] == "down" and r["z"] <= -zmin else None
            if side and ((r["ext"] == side) == want) and (out := result(r, side)):
                trades.append((out[0], out[1], r["result"] == side))
        n = len(trades)
        if not n:
            print(f"{zmin:>5} {label:>9} | 0 trades")
            continue
        p = [t[0] for t in trades]
        mean = sum(p) / n
        se = (sum((x - mean) ** 2 for x in p) / n) ** 0.5 / math.sqrt(n) * 100
        h = n // 2
        avg = lambda xs: sum(xs) / len(xs) * 100 if xs else float("nan")  # noqa: E731
        print(f"{zmin:>5} {label:>9} | {n:>6} {100 * n / len(rows):>4.0f}% | {sum(t[1] for t in trades) / n:.2f} {100 * sum(1 for t in trades if t[2]) / n:>3.0f}% | {mean * 100:>+6.1f}c +-{se:4.1f} | {avg(p[:h]):>+6.1f}c / {avg(p[h:]):>+6.1f}c")
if SCAN:
    print("\nSCAN: first new window extreme on the trend's side in minutes 12-14 (z measured then)")
    for zmin in ZMINS:
        trades = []
        for r in sorted(rows, key=lambda r: r["close"]):
            if not r["scan"] or abs(r["scan"][2]) < zmin:
                continue
            t, side, z = r["scan"]
            bid, ask = (float(r["scan_q"][0]), float(r["scan_q"][1]))
            px_ = ask if side == "yes" else 1.0 - bid
            if 0.01 < px_ < 0.99:
                trades.append(((1.0 if r["result"] == side else 0.0) - px_ - fee_per_contract(px_), px_, r["result"] == side))
        n = len(trades)
        if n:
            p = [x[0] for x in trades]
            mean = sum(p) / n
            se = (sum((x - mean) ** 2 for x in p) / n) ** 0.5 / math.sqrt(n) * 100
            print(f"  z >= {zmin:>3}: {n:>4} trades | paid {sum(x[1] for x in trades) / n:.2f} won {100 * sum(1 for x in trades if x[2]) / n:>3.0f}% | {mean * 100:>+6.1f}c +-{se:4.1f}")
print("\ncalibration, any trend: the side the index points to (z sign), by |z| bucket -- its ask against how often it won")
bk = {}
for r in rows:
    side = "yes" if r["z"] >= 0 else "no"
    out = result(r, side)
    if out:
        bk.setdefault(min(int(abs(r["z"]) / 0.5), 5), []).append((out[1], r["result"] == side, out[0]))
for k in sorted(bk):
    v = bk[k]
    print(f"  |z| {k * 0.5:>3.1f}-{k * 0.5 + 0.5:>3.1f}: {len(v):>4} windows | ask {sum(x[0] for x in v) / len(v):.2f} | won {100 * sum(1 for x in v if x[1]) / len(v):>3.0f}% | per contract at the ask {sum(x[2] for x in v) / len(v) * 100:>+5.1f}c")
print("DONE")
