"""Which exit in which market (2026-11-08, user: "the second half with +50 or higher is the setup we want"; studies/results/2026-11-08-exit-policies-by-half.txt: curve-only beat holding by +$51.88 in the second half, the stretch in which the whole market jumped).
Question: on the same 334 live crypto_multiscale bets since 11-05, split by how hot the market was at the buy (how many of the nine crypto indexes ran at 1.5x or more of their own 24 h median volatility: 0-1, 2-4, 5-9; the reading of scripts/regime_sitout_study.py), the net of: holding, the lane's actual exits, the shock exit alone, the curve rule alone (sell when the bid after the fee is over the confidence model's chance by M = 0.02, any gain or loss), and the first of the two. If the curve rule is the one that earns in the hot market and the shock exit in the calm one, the policy is a switch between them on the market's own state (rule 1, no coin names).
Not covered: real fills, the model being partly in-sample and no better than the market price, that a switch is chosen after seeing the split (said here), the number of bets per band (small in the hot band).

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/exit_policies_by_regime.py
"""

import bisect
import collections
import itertools
import math
import os
import statistics

from kalshi_moonshot import confidence, config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
model = confidence.load_model(s)
MS = (0.02,)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXNEAR15M", "KXBNB15M", "KXHYPE15M", "KXZEC15M")
_first = st.db.execute("SELECT min(ts) m, max(ts) x FROM decisions WHERE action='bet' AND mode='trade' AND lane='crypto_multiscale' AND ts>?", (1794000000,)).fetchone()
T0 = int(_first["m"]) - 86400 - 1000
T1 = int(_first["x"]) + 5
PREF = {}
for _ser in SERIES:
    _ix = variants.crypto_index(_ser)
    _r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<? AND value>0 ORDER BY ts", (_ix, T0 - 5, T1)).fetchall()
    _ts = [float(x[0]) for x in _r]
    _vs = [float(x[1]) for x in _r]
    _sq, _last, _j, _prev = [0.0], None, 0, None
    for _t in range(T0, T1):
        while _j < len(_ts) and _ts[_j] <= _t:
            _last = math.log(_vs[_j])
            _j += 1
        _sq.append(_sq[-1] + ((_last - _prev) ** 2 if _last is not None and _prev is not None else 0.0))
        _prev = _last
    PREF[_ser] = _sq


def _rms(sq, a, b):
    return math.sqrt(max(sq[b - T0] - sq[a - T0], 0.0) / (b - a))


def _ratio(ser, t):
    sq, t = PREF[ser], int(t)
    now_rms = _rms(sq, t - 900, t)
    base = [x for x in (_rms(sq, t - 900 * (k + 2), t - 900 * (k + 1)) for k in range(96)) if x > 0]
    return now_rms / statistics.median(base) if base and now_rms > 0 else 0.0


def hot_count(t):
    return sum(1 for o in SERIES if _ratio(o, t) >= 1.5)


rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.side,d.contracts,d.stake_usd,d.exit_price,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane='crypto_multiscale' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND w.result IN ('yes','no') AND w.strike>0 ORDER BY d.ts",
    (1794000000,),
).fetchall()
print("live crypto_multiscale bets with a settled window since 11-05:", len(rows))
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
    actual = (n * (float(r["exit_price"]) - fee(float(r["exit_price"]))) - cost) if r["exit_price"] else hold
    moves = collections.deque()
    shock = None
    shock_t = None
    curve = {}
    curve_t = {}
    for t in range(int(t0) + 1, int(close) - 5):
        k = t - base
        if k < 960 or k >= len(g) or g[k] is None or g[k - 60] is None:
            continue
        moves.append((t, (g[k] - g[k - 60]) * sign))
        while moves and t - moves[0][0] > 120:
            moves.popleft()
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        if shock is None and close - t <= 300 and 0.05 <= bid <= 0.89:
            rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
            if rate > 0:
                unit = rate * math.sqrt(60.0)
                if min(m for _, m in moves) <= -unit and (g[k] - K0) * sign <= 0.6 * unit:
                    shock = n * (bid - fee(bid)) - cost
                    shock_t = t
        if (t - int(t0)) % 5 == 0 and len(curve) < len(MS) and 0.05 <= bid <= 0.95:
            ask = min(0.99, bid + 0.01)
            yes_bid, yes_ask = (bid, ask) if r["side"] == "yes" else (max(0.01, 1.0 - ask), 1.0 - bid)
            f = confidence.features_at(T, V, float(t), float(r["strike"]), close - t, yes_bid, yes_ask)
            if f is not None:
                p_yes = confidence.predict(model["w"], confidence.vector(f, model["mu"], model["sd"]))
                chance = p_yes if r["side"] == "yes" else 1.0 - p_yes
                over = bid - fee(bid) - chance
                for m in MS:
                    if m not in curve and over >= m:
                        curve[m] = n * (bid - fee(bid)) - cost
                        curve_t[m] = t
        if shock is not None and len(curve) == len(MS):
            break
    out.append((t0, hold, actual, shock if shock is not None else hold, {m: curve.get(m, hold) for m in MS}, shock_t, dict(curve_t), shock, dict(curve), hot_count(t0)))
print("replayed:", len(out))
def first_of(o, m):
    st_, ct_ = o[5], o[6].get(m)
    if st_ is None and ct_ is None:
        return o[1]
    if ct_ is None or (st_ is not None and st_ <= ct_):
        return o[7]
    return o[8][m]


M = MS[0]
print(f"\nby how many of the nine crypto indexes ran at 1.5x or more of their normal volatility at the buy (curve rule M={M:.2f}):")
for label, lo, hi in (("0-1 hot (calm)", 0, 2), ("2-4 hot", 2, 5), ("5-9 hot (the whole market)", 5, 10), ("all", 0, 10)):
    part = [o for o in out if lo <= o[9] < hi]
    if not part:
        print(f"   {label}: no bets")
        continue
    print(f"   {label:28s} {len(part):3d} bets | hold {sum(o[1] for o in part):+8.2f} | lane actual {sum(o[2] for o in part):+8.2f} | shock alone {sum(o[3] for o in part):+8.2f} | curve alone {sum(o[4][M] for o in part):+8.2f} | first of the two {sum(first_of(o, M) for o in part):+8.2f}")
print("DONE")
