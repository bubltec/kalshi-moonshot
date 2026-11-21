"""RISK AS CHECKS, THE REALISTIC TEST: round trips (2026-11-21, user: "are we working on a realistic one with the new formula?"; the formula is P(risk(P)): Kalshi's own price curve is an input to the checks, the checks answer yes/no, and the answer is the price we are willing to pay).
QUESTION: on the SAME markets, bought as the lane places its orders and closed by the live commodity lanes' own exit module (commodity_exit), does each check, alone, change the round trips: (a) read at the buy, (b) as a required check (no = no order at that reading), (c) as a price mover (weak = the resting buy sits lower, in the price's own typical minutes)? Then all five directional checks at once (the formula). No fit, no blended score, no comparison with Kalshi's odds; nothing is judged held to settlement (the held result is printed beside each line only as rule 4's "what happened").
MARKETS: commodity perp markets (S&P out, Kalshi perp reference only), one bet a market (the lane's one per event). Four universes by when buying may start: the first eligible moment from minute 1, from minute 5, from minute 10 (where the live commodity lanes buy) and from minute 12 (the last 3 minutes). No cushion gate, no time gate. Readings every STEP seconds (10) to 20 s before the close.
ELIGIBLE (the lane's gate, after the checks are read): a quote at most 10 s old and the side's ask in 0.60-0.85.
THE ORDER, as the lane's: rest one cent over the side's bid for 5 s (filled when the ask comes down to it or a taker sells under it on the tape, flow_1s), then take the ask if it has not run more than 5c (buy_max_move). A MOVED order rests d typical minutes of the price under the ask until the next reading and never takes; it is not placed under 0.60. Fee on both legs (0.07 p (1-p)); stake $10 a bet.
THE EXIT (commodity_exit as it is live; the steps call the bot's own functions): under_even (curverisk.under_even, a tenth of the time left, 15 s minimum, the depth rule), trail (curverisk.trail_hit, 1 typical minute under the high since the buy, the time-aware minute pricevol.implied_rate where it reads, else the static one), time_to_go (the last 240 s, curvevalue.read under 0.5 held for max(15 s, a quarter of the time left)); a sale at the side's bid that second; otherwise settlement. never_ahead is off and late_tight is shadow in the live module, so neither sells here.
THE CHECKS (side view, market's own units): cushion (binaryrisk czb), trend (tr13), history (share of the last 30 minutes on the side's winning side), volatility (vr, unsigned), price (the side's mid now against up to 3 minutes ago, in the PRICE's typical minute: rms 5-second mid change x sqrt(12), read from 60 s of quotes on), pricedip (how far the mid is under its high of up to the last 5 minutes, in the same unit, negated: high = not dipping).
STATE (CLAUDE.md rules 2 and 3): the reading against that series' own readings of the previous 24 h IN THE SAME 3 MINUTES OF THE MARKET (both sides, 100 needed), the cutoffs recomputed hourly: ANOMALY beyond the 1st/99th percentile, else its fifth 1 (lowest) to 5; UNREAD with short history. A check says NO in fifths 1-2, WEAK in fifth 3, YES in 4-5. An anomaly sits out. An unread check has no say.
SETTINGS stated before the run, none tuned: fifths; 1 typical minute lower for a NO, half for a WEAK; the 5 s rest and 5c run are the lane's. Not covered: queue position, partial fills, the sale's own fill, the time_to_go wait's market recovery time (its 15 s default is used).
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_round_trips.py"""
import bisect, math, multiprocessing, os, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, curverisk, curvevalue, db, pricevol
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.strategy import fee_per_contract as fee

t0 = time.time()
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
STEP = int(os.environ.get("STEP", "10"))
LIMIT = int(os.environ.get("LIMIT", "0"))  # markets per index (0 = all), for a quick run
LO, HI = 0.60, 0.85
CHECKS = ("cushion", "trend", "history", "volatility", "price", "pricedip")
DIRECT = ("cushion", "trend", "history", "price", "pricedip")
QS = (0.01, 0.2, 0.4, 0.6, 0.8, 0.99)
STAKE = 10.0
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
wins = st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 7200,)).fetchall()
jobs = {}
for w in wins:
    if w["series"].startswith("KXINX"): continue
    ix = perp_index(w["series"])
    if ix: jobs.setdefault(ix, []).append((w["ticker"], w["series"], float(w["close_ts"]), w["result"] == "yes"))
if LIMIT: jobs = {k: v[-LIMIT:] for k, v in jobs.items()}


def side_val(v, name, side):
    """The check's reading in the side's view from the YES-view tuple (cz, tr, share, vr, price, dipY, dipN)."""
    sg = 1.0 if side == "yes" else -1.0
    if name == "cushion": return sg * v[0]
    if name == "trend": return sg * v[1]
    if name == "history": return v[2] if side == "yes" else 1.0 - v[2]
    if name == "volatility": return v[3]
    if name == "price": return None if v[4] is None else sg * v[4]
    return v[5] if side == "yes" else v[6]


def build(job):
    ix, ws = job
    cfg = config.reload()
    tape = db.connect(cfg.tape_path, "tape", shared=True)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, min(w[2] for w in ws) - 4900, max(w[2] for w in ws) + 5)).fetchall()
    T, Vv = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    del tk
    M, trades_seen = [], 0
    for ticker, series, close, won_yes in ws:
        opened = close - 900.0
        base = opened - 60.0  # second k of the grid is base + k; the close is k = 960
        k = bisect.bisect_left(T, opened)
        if k >= len(T) or T[k] - opened > 25.0: continue
        strike = Vv[k]
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, base, close)).fetchall()
        if len(q) < 20: continue
        qt = [float(r[0]) for r in q]
        scale = 100.0 if max(float(r[2]) for r in q) > 1.5 else 1.0
        yb, ya, age, j = [None] * 961, [None] * 961, [1e9] * 961, -1
        for kk in range(961):
            while j + 1 < len(q) and qt[j + 1] <= base + kk: j += 1
            if j >= 0: yb[kk], ya[kk], age[kk] = float(q[j][1]) / scale, float(q[j][2]) / scale, base + kk - qt[j]
        k0 = next(i for i in range(961) if yb[i] is not None) if any(x is not None for x in yb) else 961
        if k0 > 400: continue
        mid = [None if yb[i] is None else (yb[i] + ya[i]) / 2.0 for i in range(961)]
        sq = [0.0] * 961  # prefix sum of squared one-second mid changes
        for i in range(1, 961): sq[i] = sq[i - 1] + ((mid[i] - mid[i - 1]) ** 2 if mid[i - 1] is not None else 0.0)
        sell_y, buy_y = [None] * 961, [None] * 961  # the YES price a taker sold YES at (bought NO) and bought YES at, that second
        try:
            for r in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (ticker, base, close)).fetchall():
                kk = int(float(r[0]) - base)
                if 0 <= kk < 961 and r[3] is not None:
                    px = float(r[3]) / (100.0 if float(r[3]) > 1.5 else 1.0)
                    trades_seen += 1
                    if float(r[2] or 0) > 0: sell_y[kk] = px
                    if float(r[1] or 0) > 0: buy_y[kk] = px
        except Exception:
            pass
        cs = [0]
        i0 = bisect.bisect_left(T, opened - 1900.0); i1 = bisect.bisect_right(T, close)
        for v in Vv[i0:i1]: cs.append(cs[-1] + (1 if v > strike else 0))
        R = []  # (k, YES-view readings or None, the price's typical minute or None)
        t = opened + 60.0
        while t <= close - 20.0:
            kk = int(t - base)
            f = br.readings(T, Vv, t, strike, close - t) if yb[kk] is not None else None
            if f is None:
                R.append((kk, None, None)); t += STEP; continue
            je = bisect.bisect_right(T, t) - 1
            ia = max(i0, bisect.bisect_left(T, t - 1800.0))
            share = (cs[je + 1 - i0] - cs[ia - i0]) / max(1, je + 1 - ia)
            L = min(300, kk - k0)  # the price curve reads what quotes there are, up to 5 minutes
            pt = dy = dn = pu = None
            if L >= 60:
                g = [mid[kk - 5 * x] for x in range(L // 5, -1, -1)]
                ch = [b - a for a, b in zip(g, g[1:])]
                pu = math.sqrt(sum(c * c for c in ch) / len(ch) * 12.0)
                if pu > 0:
                    back = min(180, L)
                    pt = (g[-1] - mid[kk - back]) / (pu * math.sqrt(back / 60.0))
                    dy, dn = -(max(g) - g[-1]) / pu, -(g[-1] - min(g)) / pu
                else:
                    pu = None
            R.append((kk, (f["czb"], f["tr13"], share, f["vr"], pt, dy, dn), pu))
            t += STEP
        M.append({"ticker": ticker, "series": series, "close": close, "base": base, "won_yes": won_yes, "strike": strike, "yb": yb, "ya": ya, "age": age, "mid": mid, "sq": sq, "k0": k0, "sell_y": sell_y, "buy_y": buy_y, "R": R, "units": {}, "ttg": {}, "x": {}})

    # the series' own history of each check, by the 3 minutes of the market, both sides
    H = {}
    for m in M:
        for kk, v, _pu in m["R"]:
            if v is None: continue
            t, b = m["base"] + kk, min(4, (kk - 60) // 180)
            for name in CHECKS:
                for side in ("yes", "no"):
                    x = side_val(v, name, side)
                    if x is not None: H.setdefault((m["series"], b, name), []).append((t, x))
    HT = {}
    for key, v in H.items():
        v.sort()
        HT[key] = [x[0] for x in v]
    cuts = {}

    def state(m, i, name, side):
        kk, v, _pu = m["R"][i]
        x = None if v is None else side_val(v, name, side)
        if x is None: return "unread"
        t = m["base"] + kk
        key = (m["series"], min(4, (kk - 60) // 180), name)
        hour = int(t // 3600)
        c = cuts.get((key, hour), 0)
        if c == 0:
            ts = HT.get(key, [])
            own = sorted(y[1] for y in H.get(key, [])[bisect.bisect_left(ts, hour * 3600.0 - 86400.0):bisect.bisect_left(ts, hour * 3600.0)])
            c = cuts[(key, hour)] = None if len(own) < 100 else tuple(own[min(len(own) - 1, int(len(own) * p))] for p in QS)
        if c is None: return "unread"
        if x < c[0] or x > c[5]: return "anomaly"
        return 1 + sum(1 for z in c[1:5] if x > z)

    bid = lambda m, side, kk: m["yb"][kk] if side == "yes" else 1.0 - m["ya"][kk]
    ask = lambda m, side, kk: m["ya"][kk] if side == "yes" else 1.0 - m["yb"][kk]

    def rest(m, side, kk, p, w):
        """A buy resting at p from second kk for w seconds: the second it fills, or None. Filled when the side's ask comes down to p or a taker sells under p."""
        for x in range(kk + 1, min(945, kk + w) + 1):
            if ask(m, side, x) <= p + 1e-9: return x
            tr = m["sell_y"][x] if side == "yes" else (None if m["buy_y"][x] is None else 1.0 - m["buy_y"][x])
            if tr is not None and tr < p - 1e-9: return x
        return None

    def units(m, kk):
        """(static typical minute, time-aware one) of the price at second kk, read every 5 s as the watcher does."""
        kk -= kk % 5
        u = m["units"].get(kk)
        if u is None:
            n = int(min(900, kk - m["k0"] - 1))
            stat = timed = None
            if n >= 300 and m["age"][kk] <= 10.0:
                d = m["sq"][kk] - m["sq"][kk - n]
                stat = math.sqrt(d / n) * math.sqrt(60.0) if d > 0 else None
                timed = pricevol.unit_of(pricevol.implied_rate(m["mid"][kk - n:kk + 1], m["close"] - (m["base"] + kk)))
            u = m["units"][kk] = (stat, timed)
        return u

    def chance(m, side, kk):
        """curvevalue.read's chance for the side at second kk (the time_to_go step), from one read of the index."""
        c = m["ttg"].get(kk, 0)
        if c == 0:
            now = m["base"] + kk
            a = bisect.bisect_left(T, now - 1000.0)
            c = m["ttg"][kk] = curvevalue.read(T[a:bisect.bisect_right(T, now)], Vv[a:bisect.bisect_right(T, now)], now, m["strike"], "yes", m["close"] - now)
        if c is None: return None
        sg, root = (1.0 if side == "yes" else -1.0), math.sqrt(m["close"] - (m["base"] + kk))
        return min(curvevalue.phi(sg * c.cushion / (r * root)) if r > 0 else (1.0 if sg * c.cushion > 0 else 0.0) for r in (c.rate_long, c.rate_short))

    def exit_(m, side, entry, kf):
        """commodity_exit on a bet bought at second kf: (price sold at or None for settlement, step, second)."""
        key = (side, round(entry, 4), kf)
        got = m["x"].get(key)
        if got is not None: return got
        basis, peak, under, go = entry + fee(entry), 0.0, 0.0, 0.0
        out = (None, "settled", 960)
        for kk in range(kf + 1, 953):
            now, left, b = m["base"] + kk, 960.0 - kk, bid(m, side, kk)
            stat, timed = units(m, kk)
            if b >= curverisk.EVEN: under = 0.0
            elif under <= 0: under = now
            if b < curverisk.EVEN and curverisk.under_even(b, entry, now - under, left, 0.1, stat):
                out = (b, "under_even", kk); break
            peak = max(peak, b)
            if curverisk.trail_hit(b - fee(b) - basis, fee(b), b, peak, timed or stat, 1.0):
                out = (b, "trail", kk); break
            if b > 0 and 8.0 <= left <= 240.0:
                p = chance(m, side, kk)
                if p is None or p >= 0.5: go = 0.0
                else:
                    if go <= 0: go = now
                    if now - go >= max(15.0, 0.25 * left):
                        out = (b, "time_to_go", kk); break
            else:
                go = 0.0
        m["x"][key] = out
        return out

    def trade(m, rule, first_k):
        """One bet a market under a buy rule: rule(m, i, side) is None (no order at this reading) or the typical minutes the resting buy sits under the ask (0 = the lane's own order)."""
        for i, (kk, v, pu) in enumerate(m["R"]):
            if kk < first_k or v is None or m["age"][kk] > 10.0: continue
            for side in ("yes", "no"):
                a = ask(m, side, kk)
                if not LO <= a <= HI: continue
                d = rule(m, i, side)
                if d is None: continue
                price = kf = None
                if d <= 0 or pu is None:
                    p = round(min(a, bid(m, side, kk) + 0.01), 4)
                    kf = rest(m, side, kk, p, 5) if p < a - 1e-9 else kk + 1
                    price = p
                    if kf is None and kk + 5 <= 945 and ask(m, side, kk + 5) <= a + 0.05 + 1e-9:
                        price, kf = ask(m, side, kk + 5), kk + 5
                else:
                    p = math.floor((a - d * pu) * 100.0 + 1e-9) / 100.0
                    if p >= LO - 1e-9:
                        kf, price = rest(m, side, kk, p, STEP), p
                if kf is None or not 0.01 <= price <= 0.99: continue
                sold, step, kx = exit_(m, side, price, kf)
                won = m["won_yes"] if side == "yes" else not m["won_yes"]
                basis = price + fee(price)
                hold = (1.0 if won else 0.0) - basis
                net = hold if sold is None else sold - fee(sold) - basis
                return (side, price, (kf - 60) / 60.0, step, net * STAKE / price, hold * STAKE / price, {n: state(m, i, n, side) for n in CHECKS}, d)
        return None

    def required(name):
        return lambda m, i, side: None if state(m, i, name, side) in ("anomaly", 1, 2) else 0.0

    def mover(name):
        def f(m, i, side):
            x = state(m, i, name, side)
            return None if x == "anomaly" else 1.0 if x in (1, 2) else 0.5 if x == 3 else 0.0
        return f

    def formula(m, i, side):
        xs = [state(m, i, n, side) for n in DIRECT]
        if "anomaly" in xs: return None
        return sum(1.0 if x in (1, 2) else 0.5 if x == 3 else 0.0 for x in xs)

    def formula_req(m, i, side):  # the formula with a NO from most of the checks as a refusal: three or more of the five say NO = no order
        xs = [state(m, i, n, side) for n in DIRECT]
        if "anomaly" in xs or sum(1 for x in xs if x in (1, 2)) >= 3: return None
        return sum(1.0 if x in (1, 2) else 0.5 if x == 3 else 0.0 for x in xs)

    RULES = {"base": lambda m, i, side: 0.0, "formula": formula, "formula_req": formula_req}
    for n in DIRECT:
        RULES["req:" + n], RULES["move:" + n] = required(n), mover(n)
    out = []
    for m in M:
        for tag, first_k in (("any", 120), ("m5", 360), ("m10", 660), ("late", 780)):
            for rn, rule in RULES.items():
                got = trade(m, rule, first_k)
                if got: out.append((tag, rn, m["ticker"], m["close"], *got))
    return out, len(M), trades_seen


RES, NM, NT = {}, 0, 0
with multiprocessing.get_context("fork").Pool(4) as pool:
    for out, nm, nt in pool.imap_unordered(build, list(jobs.items())):
        NM += nm; NT += nt
        for r in out: RES.setdefault((r[0], r[1]), {})[r[2]] = r
        print(f"  an index done: {nm} markets, {time.time() - t0:.0f}s", flush=True)
print(f"{NM} markets, {NT} seconds of trades on the tape (flow_1s; 0 = fills from the quotes only); readings every {STEP}s; {time.time() - t0:.0f}s", flush=True)
# a bet row: 0 universe, 1 rule, 2 ticker, 3 close, 4 side, 5 price, 6 minute bought, 7 exit step, 8 net $, 9 held $, 10 states, 11 units moved


def line(g):
    n = len(g)
    if n < 20: return "n %4d (too few)" % n
    by = {}
    for x in g: by[x[7]] = by.get(x[7], 0) + 1
    return "n %4d paid %.3f min %4.1f | round trip %+8.2f$ (%+5.1fc per $1 staked) ahead %2.0f%% | held instead %+8.2f$ | sold: %s" % (
        n, sum(x[5] for x in g) / n, sum(x[6] for x in g) / n, sum(x[8] for x in g), 100.0 * sum(x[8] for x in g) / (STAKE * n), 100.0 * sum(1 for x in g if x[8] > 0) / n, sum(x[9] for x in g),
        " ".join(f"{k} {v}" for k, v in sorted(by.items())))


def shape(x):
    v = x[10]["volatility"]
    return "calm" if v in (1, 2) else "jumpy" if v in (4, 5) else "middle" if v == 3 else str(v)


for tag, title in (("any", "THE FIRST BUY FROM MINUTE 1"), ("m5", "THE FIRST BUY FROM MINUTE 5"), ("m10", "THE FIRST BUY FROM MINUTE 10 (where the live commodity lanes buy)"), ("late", "THE FIRST BUY IN THE LAST 3 MINUTES")):
    base = RES.get((tag, "base"), {})
    B = sorted(base.values(), key=lambda x: x[3])
    print(f"\n######## {title}: the lane's order on every market, closed by commodity_exit")
    print("  ALL     : " + line(B))
    for sh in ("calm", "middle", "jumpy", "anomaly", "unread"):
        print(f"  {sh:8s}: " + line([x for x in B if shape(x) == sh]))
    print("\n  (a) EACH CHECK READ AT THE BUY, on those same bets (its own-history fifth; 1-2 = NO, 3 = WEAK, 4-5 = YES)")
    for name in CHECKS:
        print(f"  --- {name}")
        for stt in (1, 2, 3, 4, 5, "anomaly", "unread"):
            print(f"    {str(stt):8s}: " + line([x for x in B if x[10][name] == stt]))
    print("\n  (b) and (c) EACH CHECK AS A RULE, against the lane's order on the same markets (a market with no bet counts 0)")
    for rn in [f"{k}:{n}" for n in DIRECT for k in ("req", "move")] + ["formula", "formula_req"]:
        V = RES.get((tag, rn), {})
        keys = set(base) | set(V)
        diff = [(V[k][8] if k in V else 0.0) - (base[k][8] if k in base else 0.0) for k in keys if (k in V) != (k in base) or (k in V and (V[k][4], V[k][5], V[k][6]) != (base[k][4], base[k][5], base[k][6]))]
        print(f"  --- {rn}: change {sum(diff):+8.2f}$ on {len(diff)} markets whose bet differs (better {sum(1 for d in diff if d > 1e-9)}, worse {sum(1 for d in diff if d < -1e-9)}); no bet on {len(base) - len(set(base) & set(V))} of the lane's {len(base)}; moved orders filled {sum(1 for x in V.values() if x[11] > 0)}")
        print("    all     : " + line(list(V.values())))
        for sh in ("calm", "middle", "jumpy"):
            ks = [k for k in keys if shape(base.get(k) or V[k]) == sh]
            print(f"    {sh:8s}: change {sum((V[k][8] if k in V else 0.0) - (base[k][8] if k in base else 0.0) for k in ks):+8.2f}$ on {len(ks)} markets | " + line([V[k] for k in ks if k in V]))
print(f"\ndone in {time.time() - t0:.0f}s")
