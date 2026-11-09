"""The shock exit checked against the curve (2026-11-08, user: "besides the shock" the curve should be looked at; HYPE 21:45 NO sold 0.56 by the shock exit with the curve's chance 0.62 over the bid 0.57).
Rule (a grid fixed before the results): when the shock exit's conditions hold (the live rule: the worst 60 s move over 120 s at least 1.0 typical minutes against the bet, the cushion within 0.6 typical minutes, last 300 s, bid 0.05-0.89), look at the confidence model's chance that the side pays (the model the value exit uses) at that second and sell only if the bid after the fee is not under the chance by more than K, i.e. over = bid - fee - chance >= -K; otherwise keep holding and look again the next second. K in 0.02, 0.05, 0.10. K = infinity is the current exit (sell at the first second the conditions hold).
Replay on the live crypto_multiscale bets since 11-05 with a settled window (contracts and side from the decision; strike and result from the window): the sale at the displayed bid less the full fee; a bet the rule never sells is held to the result. Per K: net against holding and against the current shock exit (the difference per bet it changes, with a standard error), the bets it sells later, the ones it never sells, the two halves. The model is the current one (partly in-sample, and no better than the market price: Brier 0.1225 against 0.1202).
Not covered: real fills, the value exit running alongside.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/shock_curve_check_replay.py
"""

import bisect
import collections
import itertools
import math
import os

from kalshi_moonshot import confidence, config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
model = confidence.load_model(s)
KS = (0.02, 0.05, 0.10)
rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.side,d.contracts,d.stake_usd,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane='crypto_multiscale' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND w.result IN ('yes','no') AND w.strike>0 ORDER BY d.ts",
    (1794000000,),
).fetchall()
print("live crypto_multiscale bets with a settled window since 11-05:", len(rows), "| model:", "loaded" if model else "MISSING")
out = []
for r in rows:
    ix = variants.crypto_index(r["ticker"].split("-")[0])
    if not ix or not model:
        continue
    close, t0 = float(r["close_ts"]), float(r["ts"])
    K0 = math.log(float(r["strike"]))
    sign = 1.0 if r["side"] == "yes" else -1.0
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 3700, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 5, close)).fetchall()
    if len(tk) < 1500 or len(q) < 5:
        continue
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    qt = [float(x[0]) for x in q]
    base = int(T[0])
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T) and T[j] <= t:
            last = V[j]
            j += 1
        g.append(math.log(last) if last else None)
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    moves = collections.deque()
    first = None
    done = {}
    for t in range(int(t0), int(close) - 5):
        k = t - base
        if k < 960 or k >= len(g) or g[k] is None or g[k - 60] is None:
            continue
        moves.append((t, (g[k] - g[k - 60]) * sign))
        while moves and t - moves[0][0] > 120:
            moves.popleft()
        if close - t > 300:
            continue
        rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
        if rate <= 0:
            continue
        unit = rate * math.sqrt(60.0)
        if not (min(m for _, m in moves) <= -unit and (g[k] - K0) * sign <= 0.6 * unit):
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        if not (0.05 <= bid <= 0.89):
            continue
        net = n * (bid - fee(bid)) - cost
        if first is None:
            first = net
        ask = min(0.99, bid + 0.01)
        yes_bid, yes_ask = (bid, ask) if r["side"] == "yes" else (max(0.01, 1.0 - ask), 1.0 - bid)
        f = confidence.features_at(T, V, float(t), float(r["strike"]), close - t, yes_bid, yes_ask)
        if f is None:
            continue
        p_yes = confidence.predict(model["w"], confidence.vector(f, model["mu"], model["sd"]))
        chance = p_yes if r["side"] == "yes" else 1.0 - p_yes
        over = bid - fee(bid) - chance
        for kk in KS:
            if kk not in done and over >= -kk:
                done[kk] = net
        if first is not None and len(done) == len(KS):
            break
    out.append((t0, hold, first, {kk: done.get(kk) for kk in KS}))
print("replayed:", len(out), "| the current shock exit sells:", sum(1 for o in out if o[2] is not None))
mid = sorted(o[0] for o in out)[len(out) // 2]


def ms(xs):
    n = len(xs)
    if n == 0:
        return "n=0"
    m = sum(xs) / n
    se = (sum((x - m) ** 2 for x in xs) / max(n - 1, 1)) ** 0.5 / math.sqrt(n)
    return f"{m:+.3f} +/- {se:.3f} (n={n})"


cur = [(o[2] if o[2] is not None else o[1]) for o in out]
print(f"\nheld to the result {sum(o[1] for o in out):+.2f} | the current shock exit {sum(cur):+.2f}")
for kk in KS:
    new = [(o[3][kk] if o[3][kk] is not None else o[1]) for o in out]
    ch = [(n_ - c_, o) for n_, c_, o in zip(new, cur, out, strict=True) if abs(n_ - c_) > 1e-9]
    later = sum(1 for o in out if o[2] is not None and o[3][kk] is not None and o[3][kk] != o[2])
    never = sum(1 for o in out if o[2] is not None and o[3][kk] is None)
    a = [d for d, o in ch if o[0] <= mid]
    b = [d for d, o in ch if o[0] > mid]
    print(f"  K={kk:.2f}: net {sum(new):+.2f} | against the current exit {sum(d for d, _ in ch):+.2f} on {len(ch)} changed bets (per changed bet {ms([d for d, _ in ch])}; sold later {later}, never {never}) | halves {sum(a):+.2f} (n={len(a)}) / {sum(b):+.2f} (n={len(b)})")
print("DONE")
