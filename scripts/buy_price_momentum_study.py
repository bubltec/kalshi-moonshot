"""WHAT PRICE SHOULD THE BUY LIMIT BE: at each crypto_favorite bet, the side's ask at the decision, how far it moved over the next L seconds (the order's trip), and how well the momentum of the last W seconds (5-10) predicted that move. Compares limits: ask + fixed 2c, ask + the momentum projection (v * L, never below 0), and the ask itself, by the share of orders whose limit would have been under the ask when the order landed (a miss) and the average overpay. Places nothing.
    docker compose exec -T bot python - < scripts/buy_price_momentum_study.py   (LANE=crypto_favorite SINCE_DAYS=7)
"""
import bisect
import os
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
lane = os.environ.get("LANE", "crypto_favorite")
days = float(os.environ.get("SINCE_DAYS", "7"))
rows = st.db.execute("SELECT ts, ticker, side, ask FROM decisions WHERE lane=? AND action='bet' AND ts > extract(epoch from now()) - ?*86400", (lane, days)).fetchall()
data = []
for r in rows:
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], r["ts"] - 20, r["ts"] + 6)).fetchall()
    if len(q) < 4:
        continue
    ts = [x[0] for x in q]
    px = [x[2] if r["side"] == "yes" else 1 - x[1] for x in q]  # the ask of the side
    mid = [((x[1] + x[2]) / 2 if r["side"] == "yes" else 1 - (x[1] + x[2]) / 2) for x in q]

    def at(t, series, ts=ts):
        i = bisect.bisect_right(ts, t) - 1
        return series[i] if i >= 0 else None

    data.append((r["ts"], at, px, mid))
print(len(data), "bets with quotes")
for L in (1.0, 2.0, 3.0):
    for W in (5, 10):
        ex = []  # (projection, actual move of the ask over the next L seconds)
        for t0, at, px, mid in data:
            a0, a1, m0, m1 = at(t0, px), at(t0 + L, px), at(t0, mid), at(t0 - W, mid)
            if None in (a0, a1, m0, m1):
                continue
            ex.append(((m0 - m1) / W * L, a1 - a0))
        if len(ex) < 30:
            continue
        proj, real = zip(*ex, strict=True)
        mean_p, mean_r = statistics.mean(proj), statistics.mean(real)
        cov = sum((p - mean_p) * (r - mean_r) for p, r in ex) / len(ex)
        corr = cov / (statistics.pstdev(proj) * statistics.pstdev(real)) if statistics.pstdev(proj) > 0 and statistics.pstdev(real) > 0 else 0.0

        def eval_limit(f):
            miss = sum(1 for p, r in ex if f(p) + 1e-9 < r)  # the ask moved up past our limit
            over = statistics.mean(max(0.0, f(p) - max(r, 0.0)) for p, r in ex)  # allowance we offered above what was needed
            return 100 * miss / len(ex), 100 * over
        out = [f"{name}: miss {m:4.1f}% overshoot {o:4.2f}c" for name, (m, o) in (("ask only", eval_limit(lambda p: 0.0)), ("ask+2c", eval_limit(lambda p: 0.02)), ("ask+momentum", eval_limit(lambda p: max(0.0, p))), ("ask+1.5x momentum", eval_limit(lambda p: max(0.0, 1.5 * p))))]
        print(f"L={L:.0f}s W={W:2d}s n={len(ex)} corr(projection, next move)={corr:+.2f}; " + " | ".join(out))
