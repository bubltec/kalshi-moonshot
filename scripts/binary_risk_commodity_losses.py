"""FAVOURITES BAND 0.60-0.85 (user, 2026-11-20: "run the commodity picks in the .60-.85 range"): the bar is the top 5% / 10% of the training predictions inside 0.60-0.85 and only asks inside it are picked or scored in part B. POOLED=1 fits on crypto and commodity.
COMMODITY-ONLY FIT (2026-11-20, user: "start the commodity fit because something is off"): the pooled fit showed no commodity lift (studies/results/2026-11-20-binary-risk-commodity-perp.txt). Here each hour refits the same model on commodity rows ONLY (curve readings, no asset setting: the training set is every settled commodity market, a market kind chosen by its feed, not a rule keyed on a name), trailing 72 h instead of 24 so there are enough rows. Everything else as below.
PERP VERSION (2026-11-20, user: "use the kalshi commodity perp"): the same study with commodity curves read from Kalshi's perp reference (KP_*), not BiQuote. How the binary risk model (binaryrisk.py, the capped V5) would have done on the live commodity lanes' real bets, and on the commodity markets it would have picked instead.
Question (user, 2026-11-20: "we should see how this new binary risk would do on the commodity lanes because they are failing too often with big losses and picking bad markets to play"): two parts, each walk-forward (the model is refit every hour on the trailing 24 hours of every market, crypto and commodity pooled; it names no asset and has no asset setting: rule 1).
A. The SAME bets (CLAUDE.md rule 4): every real commodity bet since Monday 11-09 (decisions with mode 'trade', stake over $0.50, lane name starting with 'commodity'), scored at the moment the bet was made, for the side it bought and the ask it paid, from the commodity's own price feed (the index the lane reads) against that feed's own price when the window opened. The model's verdict: would it have bought this side (its capped predicted net per $1 in the top 5% (and top 10%) of that hour's training sides, ask not 0.45-0.55, not under 0.20)? Against what actually happened on those bets (the lane's own exit, fee on the buy and on a sale): n, win rate, net dollars and net per $1 staked for the bets the model keeps and for the bets it would have vetoed, how many vetoed bets lost (the veto beat what happened) and how many won (the veto lost to it); by lane; and by third of the curve's cushion, path efficiency and speed-up (the bet's own side-oriented reading, cutoffs the 33rd and 67th percentiles of every commodity side reading).
B. The markets it would have PICKED: the top 5% of the hour's training sides among every commodity reading (every 60 s, both sides), first reading per market side, held to settlement, net per $1 and the lift over a random commodity point in the same ask band, in the two halves of the test hours. The ask band edges are 0.20, 0.35, 0.45, 0.55, 0.65, 0.80.
Not asked and not done here: a new exit. Settings (top 5% and 10%, the thirds) were fixed before the run. Commodity windows have no Kalshi settlement index in the tape; their strike is that price feed's value at the window's open (the first tick within 25 s), as the curve model reads them.
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_commodity.py"""
import os, array, bisect, math, multiprocessing, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as pidx
from kalshi_moonshot.perpref import index_for_series as bq_index  # Kalshi perp reference, not BiQuote (user, 2026-11-20)
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
from kalshi_moonshot.strategy import fee_per_contract as fee

t0 = time.time()
POOLED = os.environ.get('POOLED') == '1'
BAND = (0.60, 0.85)  # favourites only (user, 2026-11-20: "run the commodity picks in the .60-.85 range")
s = config.reload()
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()  # Monday 11-09 00:00 ET: the logic rewrite
STEP = 60.0
TOPS = (0.05, 0.10)
BANDS = ((0.20, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.80), (0.80, 1.01))
HORIZONS = br.HORIZONS

st = db.connect(s.db_path, "main", shared=True)
allw = st.execute("SELECT close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts IS NOT NULL").fetchall()
wins = st.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 3600 * 2,)).fetchall()
bets = st.execute("SELECT ts, lane, ticker, side, stake_usd, ask, outcome, exit_price, exit_ts FROM decisions WHERE mode='trade' AND action='bet' AND stake_usd>0.5 AND ts > ? AND lane LIKE 'commodity%' AND outcome IN ('yes','no') ORDER BY ts", (START,)).fetchall()
print(f"{len(wins)} settled windows, {len(bets)} live commodity bets", flush=True)

jobs = {}  # index -> (windows, bets)
for w in wins:
    series = w["series"]
    ix = crypto_index(series)
    kind = "crypto" if ix else "commodity"
    if not ix:
        ix = bq_index(series)
    if ix:
        jobs.setdefault(ix, ([], []))[0].append((w["ticker"], float(w["close_ts"]), float(w["strike"]) if (kind == "crypto" and w["strike"]) else None, w["result"] == "yes", kind))
win_close = {w["ticker"]: float(w["close_ts"]) for w in wins}
for b in bets:
    series = b["ticker"].split("-")[0]
    ix = bq_index(series)
    if ix and b["ticker"] in win_close:
        jobs.setdefault(ix, ([], []))[1].append((float(b["ts"]), b["lane"], b["ticker"], b["side"], float(b["stake_usd"]), float(b["ask"]), b["outcome"], b["exit_price"], b["exit_ts"]))

def build(job):
    """One index's training sides (every STEP seconds of its settled windows) and the readings at its live bets."""
    ix, (ws, bs) = job
    s_ = config.reload()
    tape = db.connect(s_.tape_path, "tape", shared=True)
    lo = min(w[1] for w in ws) - 900 - 4000
    hi = max(w[1] for w in ws) + 5
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, lo, hi)).fetchall()
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    del tk
    rows, betf = [], []
    for ticker, close, strike, won_yes, kind in ws:
        opened = close - 900.0
        if strike is None:  # a commodity: the feed's own price when the window opened
            k = bisect.bisect_left(T, opened)
            if k >= len(T) or T[k] - opened > 25.0:
                continue
            strike = V[k]
        ref = []  # the spot 24 h and 72 h before the open: the drift readings' references
        for hh in (24.0, 72.0):
            rr = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (ix, opened - hh * 3600.0, opened - hh * 3600.0 - 1800.0)).fetchone()
            ref.append(float(rr[0]) if rr else 0.0)
        refs = (ref[0], ref[1])
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, opened - 60, close)).fetchall()
        if len(q) < 20:
            continue
        qt, qb, qa = [float(r[0]) for r in q], [float(r[1]) for r in q], [float(r[2]) for r in q]
        scale = 100.0 if max(qa) > 1.5 else 1.0

        def quote_at(t, qt=qt, qb=qb, qa=qa, scale=scale):
            j = bisect.bisect_right(qt, t) - 1
            return (qb[j] / scale, qa[j] / scale) if j >= 0 and t - qt[j] <= 60.0 else None

        for b in [b for b in bs if win_close.get(b[2]) == close]:  # this window's live bets
            f = br.readings(T, V, b[0], strike, close - b[0], refs)
            if f:
                betf.append((b, f))
        t = opened + 60.0
        while t <= close - 15.0:
            qq = quote_at(t)
            f = br.readings(T, V, t, strike, close - t, refs) if qq else None
            if f and qq:
                ahead = [quote_at(t + h) if t + h <= close - 15.0 else None for h in HORIZONS]
                for side, ask in (("yes", qq[1]), ("no", 1.0 - qq[0])):
                    if not 0.02 <= ask <= 0.98:
                        continue
                    won = won_yes if side == "yes" else not won_yes
                    pays = [1.0 if won else 0.0]
                    for e in ahead:
                        if e is None:
                            pays.append(1.0 if won else 0.0)
                        else:
                            sell = e[0] if side == "yes" else 1.0 - e[1]
                            pays.append(sell - fee(sell))
                    rows.append((t, kind, br.side_vector(f, side, ask), [(p - ask - fee(ask)) / ask for p in pays], ask, ticker, side))
            t += STEP
    return rows, betf

with multiprocessing.get_context("fork").Pool(4) as pool:
    built = []
    for i, res in enumerate(pool.imap_unordered(build, list(jobs.items()))):
        built.append(res)
        print(f"built {i + 1}/{len(jobs)} indexes, {sum(len(r[0]) for r in built)} sides, {sum(len(r[1]) for r in built)} bets read, {time.time() - t0:.0f}s", flush=True)
S = sorted((r for b in built for r in b[0]), key=lambda r: r[0])
BF = sorted((x for b in built for x in b[1]), key=lambda x: x[0][0])
print(f"{len(S)} training sides ({sum(1 for r in S if r[1] == 'commodity')} commodity), {len(BF)} of {len(bets)} live bets could be read", flush=True)

NI = {n: i for i, n in enumerate(br.NAMES)}
SH = [("cushion", NI["cz3"]), ("30-min place", NI["pos30"]), ("30-min move", NI["mv30"])]
cmd = [r for r in S if r[1] == "commodity"][::5]
cuts = []
for _, i in SH:
    v = sorted(r[2][i] for r in cmd)
    cuts.append((v[len(v) // 3], v[2 * len(v) // 3]))
def terc(x, k):
    lo, hi = cuts[k]; v = x[SH[k][1]]
    return 0 if v < lo else 1 if v < hi else 2
TN = ("low", "mid", "high")

def band_of(a):
    for k, (lo, hi) in enumerate(BANDS):
        if lo <= a < hi: return k

def actual(b):
    """What happened on a live bet: net dollars with the lane's own exit (fee on the buy and on a sale), and held to settlement."""
    ts, lane, ticker, side, stake, ask, outcome, ep, ets = b
    won = outcome == side
    held = (1.0 if won else 0.0) - ask - fee(ask)
    if ep is not None and ets is not None:
        sold = float(ep) - fee(float(ep)) - ask - fee(ask)
    else:
        sold = held
    return stake * sold / ask, stake * held / ask, won

times = [r[0] for r in S]
bt = [x[0][0] for x in BF]
t_start = S[0][0] + 24 * 3600
t_end = S[-1][0]
t_mid = (t_start + t_end) / 2
verdicts = {top: [] for top in TOPS}  # per live bet: (lane, kept, net_lane, net_held, won, shape thirds, score)
picks = {top: {} for top in TOPS}
EXS = []
base = {}
t = t_start
nh = 0
while t + 3600 <= t_end:
    a0, a1, b1 = (bisect.bisect_left(times, x) for x in (t - 72 * 3600, t, t + 3600))
    tr, te = [r for r in S[a0:a1] if (POOLED or r[1] == 'commodity')], S[a1:b1]  # commodity-only fit on the trailing 72 h (POOLED=1: crypto and commodity, 72 h)
    half = 0 if t < t_mid else 1
    j0, j1 = bisect.bisect_left(bt, t), bisect.bisect_left(bt, t + 3600)
    hour_bets = BF[j0:j1]
    t += 3600
    if len(tr) < 4000 or len(te) < 600:
        continue
    rows = [(r[2], r[3], r[4]) for r in tr]
    model = br.fit(rows)
    nh += 1
    ps = sorted(br.predict_capped(model, r[2], r[4])[0] for r in tr[::4] if br.usable(r[4]) and BAND[0] <= r[4] < BAND[1])  # the bar comes from the favourites band's own predictions
    cut = {top: ps[int(len(ps) * (1 - top))] for top in TOPS}
    for b, f in hour_bets:  # Part A
        side, ask = b[3], b[5]
        x = br.side_vector(f, side, ask)
        pred = br.predict_capped(model, x, ask)[0] if br.usable(ask) else -9.0
        net_lane, net_held, won = actual(b)
        for top in TOPS:
            verdicts[top].append((b[1], pred >= cut[top], net_lane, net_held, won, [terc(x, k) for k in range(len(SH))], b[4]))
    for r in te:  # Part B: commodity readings only
        if r[1] != "commodity" or not br.usable(r[4]) or not BAND[0] <= r[4] < BAND[1]:
            continue
        k = band_of(r[4]); c = base.setdefault((half, k), [0, 0.0]); c[0] += 1; c[1] += r[3][0]
        pr = br.predict_capped(model, r[2], r[4])[0]
        for top in TOPS:
            if pr >= cut[top] and (r[5], r[6]) not in picks[top]:
                picks[top][(r[5], r[6])] = (half, k, r[3][0])
                if top == TOPS[0]: EXS.append((r[0], r[5], r[6], r[4], pr, br.EXITS[br.predict_capped(model, r[2], r[4])[1]], r[2], r[3][0], half))
print(f"{nh} test hours\n")
import random as _rnd

print("=== A. the live commodity bets, scored at the moment they were made (walk-forward; what actually happened = the lane's own exit, fee on both legs)")
for top in TOPS:
    V = verdicts[top]
    print(f"\n-- bar: top {int(top * 100)}% of the hour's training sides ({len(V)} bets inside the test hours)")
    def line(name, g):
        if not g:
            return f"  {name:22s} n 0"
        n = len(g); stake = sum(x[6] for x in g); net = sum(x[2] for x in g); held = sum(x[3] for x in g)
        return f"  {name:22s} n {n:4d} win {sum(1 for x in g if x[4]) / n:.2f} staked ${stake:7.0f} net ${net:+8.0f} ({net / stake:+.3f} per $1) | held to settlement ${held:+8.0f}"
    print(line("all live bets", V)); print(line("kept (would buy)", [x for x in V if x[1]])); print(line("vetoed (would not buy)", [x for x in V if not x[1]]))
    vet = [x for x in V if not x[1]]
    print(f"  vetoed bets that LOST (the veto beat what happened): {sum(1 for x in vet if x[2] < 0)} worth ${sum(x[2] for x in vet if x[2] < 0):+.0f}; vetoed bets that WON (the veto lost to it): {sum(1 for x in vet if x[2] >= 0)} worth ${sum(x[2] for x in vet if x[2] >= 0):+.0f}")
    print("  by lane (kept | vetoed): net $ per $1 staked")
    for lane in sorted({x[0] for x in V}):
        gk, gv = [x for x in V if x[0] == lane and x[1]], [x for x in V if x[0] == lane and not x[1]]
        f_ = lambda g: f"n {len(g):3d} ${sum(x[2] for x in g):+7.0f} ({sum(x[2] for x in g) / max(sum(x[6] for x in g), 1e-9):+.2f})" if g else "n   0"
        print(f"    {lane:22s} {f_(gk)} | {f_(gv)}")
    for k, (nm, _) in enumerate(SH):
        for q in range(3):
            g = [x for x in V if x[5][k] == q]
            if g:
                gv = [x for x in g if not x[1]]
                print(f"  {nm:16s} {TN[q]:4s}: all n {len(g):4d} ${sum(x[2] for x in g):+7.0f} | vetoed n {len(gv):4d} ${sum(x[2] for x in gv):+7.0f}")

print("\n=== B. the commodity markets the model would have picked (held to settlement; lift over a random commodity point in the same ask band)")
for top in TOPS:
    for half in (0, 1):
        v = [(x[2], x[2] - base[(half, x[1])][1] / base[(half, x[1])][0]) for x in picks[top].values() if x[0] == half and base.get((half, x[1]))]
        if v:
            n = len(v); m = sum(a for a, _ in v) / n; l = sum(b for _, b in v) / n
            sd = math.sqrt(max(0.0, sum(b * b for _, b in v) / n - l * l))
            print(f"  top {int(top * 100):2d}% {'first' if half == 0 else 'second'} half: n {n:5d} net {m:+.3f} lift {l:+.3f}+-{sd / math.sqrt(n):.3f}")
    cnt = {}
    for x in picks[top].values(): cnt[x[1]] = cnt.get(x[1], 0) + 1
    print(f"  top {int(top * 100):2d}% picks by ask band: " + ", ".join(f"{BANDS[k][0]:.2f}-{min(BANDS[k][1], 1.0):.2f}: {cnt.get(k, 0)}" for k in sorted(cnt)))
print(f"done in {time.time() - t0:.0f}s")

print("\n=== LOSING BUYS: the top-5% picks in 0.60-0.85 that lost, 20 drawn by a fixed seed. For each: what it bought and why, then what the market did: the side's own cushion (in the market's typical moves; negative = the spot is on the wrong side of the strike) and the side's ask, every 60 s from the buy to the close")
sidx = {w["ticker"]: w["series"] for w in wins}
rr = _rnd.Random(11)
losses = [e for e in EXS if e[7] < 0]
print(f"{len(losses)} losing picks of {len(EXS)}")
tape = db.connect(s.tape_path, "tape", shared=True)
for t, tk, sd, ask, pr, ex, vec, net, half in sorted(rr.sample(losses, min(20, len(losses))), key=lambda x: x[0]):
    nm = dict(zip(br.NAMES, vec)); series = sidx[tk]; ix = pidx(series) or crypto_index(series)
    close = next(float(w["close_ts"]) for w in wins if w["ticker"] == tk)
    tkk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, close - 900 - 4000, close + 5)).fetchall()
    T, V = [float(r[0]) for r in tkk], [float(r[1]) for r in tkk]
    k = bisect.bisect_left(T, close - 900); strike = V[k] if k < len(T) else None
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (tk, t - 60, close)).fetchall()
    qt = [float(r[0]) for r in q]; sc = 100.0 if q and max(float(r[2]) for r in q) > 1.5 else 1.0
    cz, aa = [], []
    for tt in [t + 60.0 * i for i in range(0, 15) if t + 60.0 * i <= close - 15.0]:
        f = br.readings(T, V, tt, strike, close - tt) if strike else None
        cz.append("%+.1f" % ((1 if sd == "yes" else -1) * f["cz"]) if f else "-")
        j = bisect.bisect_right(qt, tt) - 1
        aa.append("%.2f" % ((float(q[j][2]) if sd == "yes" else 1.0 - float(q[j][1])) / sc) if j >= 0 else "-")
    print(f"\n{datetime.fromtimestamp(t, timezone.utc).strftime('%m-%d %H:%MZ')} {tk} buy {sd.upper()} at {ask:.2f}: predicted {pr:+.2f}/$1 by {ex}; at the buy: cushion {nm['cz3']:+.1f}, trend {nm['tr13']:+.2f}, 30-min place {nm['pos30']:.2f}, {nm['left']:.1f} min left; lost {net:+.2f} per $1")
    print("   side cushion each minute: " + " ".join(cz))
    print("   side ask each minute:     " + " ".join(aa))
