"""The risk score with the factors the user kept, and what it looks like on two of today's bets (2026-11-16, user: "risk score -- lets break it down"; "remove Place in its own range, Long layer, What was paid, What is at stake -- we don't care about these. now lets look at what its looks like").

The score is the chance a side LOSES. Factors kept, each for the YES side at a moment and each read only from data stamped at or before it:
  price      logit of Kalshi's mid (the market's own view)
  cushion    the settlement index against the target, in its typical moves for the time left
  flip rate  the number of times the favorite has changed in THIS market so far, per minute lapsed (user: "a count of the number of times it flipped compared to ... time lapsed"), times the logit of the mid: a market that keeps flipping makes the current leader's price mean less. It replaced the share of the market led, which the user called too rigid
  time       the share of the market gone x the logit of the mid
  t15 t30 t60  where the target sits in the index's range over the 15, 30 and 60 minutes before the open (0 low, 1 high), centred
  margin     the average finish of the series' last four markets against their targets, in typical moves (+ = above)
  flips      the average number of favorite changes in those four markets, less 2
(Time held belongs to a bet, not a market, so it cannot be fitted here; the typical move is the unit the cushion and the margin are measured in.)
Fit: logistic regression, walk-forward by day. Then the fitted score is run along two of today's live bets, second by second view every 30 s, beside Kalshi's own risk: ETH 16:00 YES (bought 0.91, stopped at 0.58, won) and DOGE 18:15 NO (bought 0.77, stopped at 0.36), using a fit made only on the days before today.
Nothing is bought or sold here. No coin is named in the fit.

Run on the host: scripts/cloud.sh run scripts/risk_score_fit.py
"""
import bisect
import datetime
import math
from collections import defaultdict

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.lanes import lane_specs

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
NAMES = ("price", "cushion", "flip rate", "time", "t15", "t30", "t60", "margin", "flips")
SHOW = {"KXETH15M-26NOV161600": "yes", "KXDOGE15M-26NOV161815": "no"}  # matched on the start of the ticker
show_rows = []  # (ticker, time, x, mid)
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = sorted(set(lane_specs(s)["crypto_multiscale"].series))
lg = lambda p: math.log(p / (1 - p))  # noqa: E731
rows = []  # (day, x vector, yes won, mid)
for ser in series:
    ix = variants.crypto_index(ser)
    bt, bv, last = [], [], -1
    for r in (tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, SINCE - 4000)).fetchall() if ix else []):
        b = int(float(r[0]) // 5)
        if b != last:
            bt.append((b + 1) * 5.0); bv.append(float(r[1])); last = b  # 5 s buckets stamped at their END
        else:
            bv[-1] = float(r[1])
    prev = []  # the series' finished markets so far: (margin, flips), oldest first
    for w in st.execute("SELECT ticker, close_ts, result, strike FROM windows WHERE series=? AND close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts", (ser, SINCE)).fetchall():
        close, strike = float(w["close_ts"]), float(w["strike"])
        opened = close - 900.0
        j0, i60 = bisect.bisect_right(bt, opened), bisect.bisect_right(bt, opened - 3600.0)
        if j0 - i60 < 600:
            continue
        marks = [bv[bisect.bisect_right(bt, opened - 60.0 * k) - 1] for k in range(60, -1, -1)]
        mv = [math.log(marks[k + 1] / marks[k]) for k in range(60)]
        unit = math.sqrt(sum(m * m for m in mv) / 60)  # typical one-minute move of the hour before the open
        if unit <= 0:
            continue
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened, close)).fetchall()
        q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
        if len(q) < 50:
            continue
        qt = [x[0] for x in q]
        day = datetime.datetime.fromtimestamp(close, ET).strftime("%m-%d")
        places = []
        for m in (15, 30, 60):
            a_ = bisect.bisect_right(bt, opened - 60.0 * m)
            seg = bv[a_:j0]
            places.append(((strike - min(seg)) / (max(seg) - min(seg)) - 0.5) if seg and max(seg) > min(seg) else 0.0)
        last4 = prev[-4:]
        pm = sum(p[0] for p in last4) / len(last4) if last4 else 0.0
        pf = (sum(p[1] for p in last4) / len(last4) - 2.0) if last4 else 0.0
        lo = hi = None
        n = fav = 0
        hist = {}
        side_now, nflips = None, 0  # the favorite so far in this market and how often it has changed (0.55 to take the lead)
        for t in range(int(opened), int(close) - 59, 5):
            i = bisect.bisect_right(qt, t) - 1
            if i < 0:
                continue
            mid = (q[i][1] + q[i][2]) / 2
            lo, hi = (mid, mid) if lo is None else (min(lo, mid), max(hi, mid))
            hist[t] = mid
            lead = "yes" if mid >= 0.55 else "no" if mid <= 0.45 else None
            if lead and side_now and lead != side_now:
                nflips += 1
            side_now = lead or side_now
            if (t - int(opened)) % 30 == 0 and t - opened >= 60 and n >= 6 and hi > lo and q[i][2] - q[i][1] <= 0.03 and 0.03 < mid < 0.97:
                k = bisect.bisect_right(bt, t) - 1
                back = hist.get(t - 180)
                if k >= 0 and t - bt[k] < 30 and back is not None:
                    left = close - t
                    cush = math.log(bv[k] / strike) / (unit * math.sqrt(left / 60.0))
                    x = (lg(mid), max(-6.0, min(6.0, cush)), min(1.5, nflips / max((t - opened) / 60.0, 1.0)) * lg(mid), (t - opened) / 900.0 * lg(mid), places[0], places[1], places[2], max(-3.0, min(3.0, pm)), max(-2.0, min(4.0, pf)))
                    rows.append((day, x, w["result"] == "yes", mid, t - opened))
                    key = next((k_ for k_ in SHOW if w["ticker"].startswith(k_)), None)
                    if key:
                        show_rows.append((key, t, x, mid, cush, nflips))
            n += 1
            fav += mid > 0.5
        e_a, e_b = bisect.bisect_right(bt, close - 60.0), bisect.bisect_right(bt, close)  # this market is finished: its margin and flips join the series' record
        if e_b > e_a:
            end = sum(bv[e_a:e_b]) / (e_b - e_a)
            fv, fl = None, 0
            for _, b_, a_ in q:
                m_ = (b_ + a_) / 2
                now_ = "yes" if m_ >= 0.55 else "no" if m_ <= 0.45 else None
                if now_ and fv and now_ != fv:
                    fl += 1
                fv = now_ or fv
            prev.append((math.log(end / strike) / (unit * math.sqrt(15.0)), fl))
    del bt, bv

sig = lambda z: 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))  # noqa: E731
K = len(NAMES)


def fit(data):
    w = [1.0] + [0.0] * (K - 1)
    for _ in range(30):
        g = [0.0] * K
        H = [[0.0] * K for _ in range(K)]
        for r_ in data:
            x, won = r_[1], r_[2]
            p = sig(sum(a * b for a, b in zip(w, x)))
            e, v = (1.0 if won else 0.0) - p, p * (1 - p)
            for a in range(K):
                g[a] += e * x[a]
                for b in range(a, K):
                    H[a][b] += v * x[a] * x[b]
        for a in range(K):
            H[a][a] += 1e-6
            for b in range(a):
                H[a][b] = H[b][a]
        M = [H[a][:] + [g[a]] for a in range(K)]  # solve H d = g
        for c in range(K):
            piv = max(range(c, K), key=lambda r: abs(M[r][c]))
            M[c], M[piv] = M[piv], M[c]
            for r in range(K):
                if r != c and M[c][c]:
                    f = M[r][c] / M[c][c]
                    M[r] = [u - f * v for u, v in zip(M[r], M[c])]
        d = [M[a][K] / M[a][a] if M[a][a] else 0.0 for a in range(K)]
        w = [a + b for a, b in zip(w, d)]
        if sum(abs(x) for x in d) < 1e-6:
            break
    return w


days = sorted({r[0] for r in rows})
loss = {"mid": [0.0, 0], "score": [0.0, 0]}
gap = defaultdict(lambda: [0, 0, 0.0, 0.0])  # band -> n, yes won, mid sum, score sum
BANDS = ((-9, -0.06, "score 6+ points UNDER the mid"), (-0.06, -0.03, "3 to 6 under"), (-0.03, -0.01, "1 to 3 under"), (-0.01, 0.01, "within 1 point"), (0.01, 0.03, "1 to 3 over"), (0.03, 0.06, "3 to 6 over"), (0.06, 9, "score 6+ points OVER the mid"))
w_last = None
for D in days[2:]:
    train = [r for r in rows if r[0] < D]
    if len(train) < 5000:
        continue
    w_last = fit(train)
    for r_ in (r for r in rows if r[0] == D):
        x, won, mid = r_[1], r_[2], r_[3]
        p = min(1 - 1e-6, max(1e-6, sig(sum(a * b for a, b in zip(w_last, x)))))
        loss["mid"][0] -= math.log(mid if won else 1 - mid); loss["mid"][1] += 1
        loss["score"][0] -= math.log(p if won else 1 - p); loss["score"][1] += 1
        name = next(nm for lo_, hi_, nm in BANDS if lo_ <= p - mid < hi_)
        c = gap[name]
        c[0] += 1; c[1] += won; c[2] += mid; c[3] += p

print(f"window: 2026-11-09 00:00 ET on; {len(rows)} moments on days {', '.join(days)}; scored walk-forward on {', '.join(days[2:])}")
print("weights of the last fit (trained on every day before the last): " + ", ".join(f"{n} {v:+.3f}" for n, v in zip(NAMES, w_last or [])))
print(f"log-loss on the held-out days (lower is better): Kalshi's mid {loss['mid'][0] / loss['mid'][1]:.4f} | the score {loss['score'][0] / loss['score'][1]:.4f}")
print("\nwhere the score and Kalshi's mid disagree, who was right? (YES side, held-out days)")
for _, _, name in BANDS:
    c = gap[name]
    if c[0] < 200:
        print(f"  {name:30s} n {c[0]:6d}")
        continue
    print(f"  {name:30s} n {c[0]:6d} | Kalshi's mid said {c[2] / c[0]:5.1%} | the score said {c[3] / c[0]:5.1%} | YES actually won {c[1] / c[0]:5.1%}")
print("\nthe previous four markets: their average finish against their targets, and what the next market did (moments in its first four minutes; YES side)")
early = [r for r in rows if r[4] <= 240]  # the first readings a market has (a reading needs three minutes of its quotes)
for lo_, hi_, name in ((-9, -1.0, "finished well BELOW their targets (under -1)"), (-1.0, -0.3, "below (-1 to -0.3)"), (-0.3, 0.3, "about level"), (0.3, 1.0, "above (+0.3 to +1)"), (1.0, 9, "finished well ABOVE (over +1)")):
    rs = [r for r in early if lo_ <= r[1][7] < hi_]
    if len(rs) < 200:
        print(f"  {name:46s} n {len(rs):5d}")
        continue
    print(f"  {name:46s} n {len(rs):5d} | Kalshi's mid said YES {sum(r[3] for r in rs) / len(rs):5.1%} | YES actually won {sum(1 for r in rs if r[2]) / len(rs):5.1%}")
print("and by how much those four markets flipped (average changes of favorite)")
for lo_, hi_, name in ((-9, -1.0, "under 1 flip a market"), (-1.0, 0.0, "1 to 2"), (0.0, 1.0, "2 to 3"), (1.0, 9, "3 and over")):
    rs = [r for r in early if lo_ <= r[1][8] < hi_]
    if len(rs) < 200:
        print(f"  {name:46s} n {len(rs):5d}")
        continue
    fav_won = sum(1 for r in rs if (r[3] > 0.5) == r[2]) / len(rs)
    print(f"  {name:46s} n {len(rs):5d} | the early favorite went on to win {fav_won:5.1%} | its average price {sum(max(r[3], 1 - r[3]) for r in rs) / len(rs):.3f}")
w_show = fit([r for r in rows if r[0] < days[-1]])  # for the two bets: a fit on every day before today
print("\nthe score along two of today's bets (the fit used is from the days before today)")
for tk, side in SHOW.items():
    print(f"  {tk} {side.upper()}: time | side's price | risk by Kalshi's price | risk by the score | cushion for the side | flips so far")
    for _, t_, x_, mid_, cu, nf in [r for r in show_rows if r[0] == tk]:
        p_yes = sig(sum(a * b for a, b in zip(w_show, x_)))
        ps, pk, cs = (p_yes, mid_, cu) if side == "yes" else (1 - p_yes, 1 - mid_, -cu)
        print(f"    {datetime.datetime.fromtimestamp(t_, ET).strftime('%H:%M:%S')} | {pk:.2f} | {100 * (1 - pk):3.0f}% | {100 * (1 - ps):3.0f}% | {cs:+.2f} | {nf}")
print("DONE")
