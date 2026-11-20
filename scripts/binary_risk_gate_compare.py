"""GATE COMPARISON (2026-11-20, user via parent: "test replacing the fixed 3-minute floor with one that scales with the market's own typical minute (rule 2)"). Forms fixed BEFORE the run. The cushion cz is already in units of the expected move over the time left (it divides by sqrt(minutes left)), so "the cushion exceeds the expected move over the time left" is cz >= 1. Variants of the eligibility gate, each applied to the training rows and to the picks: FIXED = every cushion (1, 3 and 5 minute averages, the side's view) above 0 and at least 180 s left (today's gate); S1 = the weakest of the three cushions >= 0.5 (half the expected move over the time left) and at least 60 s left (settlement uses the last minute: structural); S2 = the weakest >= 1.0 (the expected move) and at least 60 s left. Same bets for all: commodity picks in 0.60-0.85 (S&P out), commodity-only fit on the trailing 72 h, the bar from the band's own predictions, top 5% and 10%, held to settlement, the two halves of the test hours. Reported per variant and half: n, net per $1 after the fee, win rate; then against FIXED: picks in both, picks only the variant takes, picks only FIXED takes (n, net), and the net the variant gains or loses on the bets it adds or drops. The scaled floor replaces the fixed one only if it does at least as well in both halves.
FAVOURITES BAND 0.60-0.85 (user, 2026-11-20: "run the commodity picks in the .60-.85 range"): the bar is the top 5% / 10% of the training predictions inside 0.60-0.85 and only asks inside it are picked or scored in part B. POOLED=1 fits on crypto and commodity.
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
    if w["series"].startswith("KXINX"): continue  # the stock index is not a commodity series (user, 2026-11-20)
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
                    if not 0.02 <= ask <= 0.98 :  # no gate here: every gate variant is applied below, from the stored cushions
                        continue
                    won = won_yes if side == "yes" else not won_yes
                    pays = [1.0 if won else 0.0]
                    for e in ahead:
                        if e is None:
                            pays.append(1.0 if won else 0.0)
                        else:
                            sell = e[0] if side == "yes" else 1.0 - e[1]
                            pays.append(sell - fee(sell))
                    sg = 1.0 if side == 'yes' else -1.0
                    rows.append((t, kind, br.side_vector(f, side, ask), [(p - ask - fee(ask)) / ask for p in pays], ask, ticker, side, (sg * f['cz1'], sg * f['cz3'], sg * f['cz5'], close - t)))
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
SH = [("cushion", NI["czb"]), ("30-min place", NI["pos30"]), ("30-min move", NI["mv30"])]
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


def ok(czs, variant):
    c1, c3, c5, left = czs
    w = min(c1, c3, c5)
    if variant == "FIXED": return w > 0.0 and left >= 180.0
    return w >= (0.5 if variant == "S1" else 1.0) and left >= 60.0

times = [r[0] for r in S]
t_start = S[0][0] + 24 * 3600
t_end = S[-1][0]
t_mid = (t_start + t_end) / 2
VARS = ("FIXED", "S1", "S2")
picks = {(v, top): {} for v in VARS for top in TOPS}
for v in VARS:
    t = t_start
    while t + 3600 <= t_end:
        a0, a1, b1 = (bisect.bisect_left(times, x) for x in (t - 72 * 3600, t, t + 3600))
        tr = [r for r in S[a0:a1] if r[1] == 'commodity' and ok(r[7], v)]
        te = [r for r in S[a1:b1] if r[1] == 'commodity' and ok(r[7], v)]
        half = 0 if t < t_mid else 1
        t += 3600
        if len(tr) < 3000 or len(te) < 300:
            continue
        model = br.fit([(r[2], r[3], r[4]) for r in tr])
        ps = sorted(br.predict_capped(model, r[2], r[4])[0] for r in tr[::4] if br.usable(r[4]) and BAND[0] <= r[4] < BAND[1])
        if not ps: continue
        cut = {top: ps[int(len(ps) * (1 - top))] for top in TOPS}
        for r in te:
            if not br.usable(r[4]) or not BAND[0] <= r[4] < BAND[1]:
                continue
            pr = br.predict_capped(model, r[2], r[4])[0]
            for top in TOPS:
                if pr >= cut[top] and (r[5], r[6]) not in picks[(v, top)]:
                    picks[(v, top)][(r[5], r[6])] = (half, r[3][0], r[4])
def cell(g):
    return "n %4d net %+.3f win %.2f" % (len(g), sum(x[1] for x in g) / len(g), sum(1 for x in g if x[1] > 0) / len(g)) if g else "n    0"
print("\n=== PICKS per gate variant (commodity, 0.60-0.85, held to settlement; net per $1 after the fee)")
for top in TOPS:
    for v in VARS:
        print(f"top {int(top * 100):2d}% {v:5s}: " + " | ".join(f"half {h + 1}: " + cell([x for x in picks[(v, top)].values() if x[0] == h]) for h in (0, 1)))
print("\n=== AGAINST FIXED (the same picks compared: both / only the variant / only FIXED)")
for top in TOPS:
    fx = picks[("FIXED", top)]
    for v in VARS[1:]:
        pv = picks[(v, top)]
        both = [k for k in pv if k in fx]; only_v = [k for k in pv if k not in fx]; only_f = [k for k in fx if k not in pv]
        for h in (0, 1):
            g = lambda ks, src: [src[k] for k in ks if src[k][0] == h]
            print(f"top {int(top * 100):2d}% {v} half {h + 1}: both {cell(g(both, pv))} | only {v} {cell(g(only_v, pv))} | only FIXED {cell(g(only_f, fx))}")
print(f"done in {time.time() - t0:.0f}s")
