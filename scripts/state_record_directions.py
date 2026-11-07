"""State-record buying with curve DIRECTIONS (2026-11-07, user: the 15-minute curve too): instead of allowing one hand-picked pair of varieties (60m D + 5m F), buy ANY curve state whose own recorded result says it pays.

Question (2026-11-07, user: "why are we limiting it to specific variants? every market run is going to have a different variant"): on the same market moments the multiscale scan covers (every 30 s, both sides, every crypto and commodity 15-minute window since 11-02, ask 0.20-0.70, spread <= 0.04; crypto from minute 2 to 14, commodities from minute 10), does buying the states that paid in the EARLIER days beat the price on the LATER days?
The state is the variety letters read at 5m+60m, 5m+15m+60m, or all five scales (keys never contain a coin or an asset class). For each test day the record is every earlier day: per (state, price band) the net of a $1 stake (fee in, held to the result), one reading per market side and state, shrunk toward zero by K = 10 readings. A moment is bought when the shrunk record is at least THR with at least MIN_N earlier markets; one buy per market side, at the first such moment.
Reported: buys, win rate against the price paid, net per $10 ticket, in two halves of the test days, and by class as a label only. The fixed rule 60m D + 5m F is scored on the same days for comparison.
"""
import os, time, calendar, bisect, json, math, itertools, collections
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,11,2,6,0,0)); now=time.time()
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(t0,)).fetchall()
G0=int(t0-4300); G1=int(now)+2
LOOKS=(60,120,300,900,3600)
grids={}
def grid(ix):
    if ix in grids: return grids[ix]
    r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,G0-60)).fetchall()
    ts=[float(x[0]) for x in r]; vs=[float(x[1]) for x in r]
    g=[]; j=0; last=None
    for T in range(G0,G1):
        while j<len(ts) and ts[j]<=T: last=vs[j]; j+=1
        g.append(last)
    P=[0.0]
    for a,b in itertools.pairwise(g): P.append(P[-1]+((math.log(b/a))**2 if a and b else 0.0))
    grids[ix]=(ts,vs,g,P); return grids[ix]
def letter(z,slope,cross,thr):
    if cross>=thr: return 'G'
    if z>=1.0: return 'A' if slope>=0.5 else 'C' if slope<=-0.5 else 'B'
    return 'D' if slope<=-0.5 else 'F' if slope>=0.5 else 'E'
THR={L:max(2,round(0.8*L/60)) for L in LOOKS}
rows=[]; wid=0
for w in W:
    ser=w['series']
    if ser in variants.RECORD_ONLY: continue
    cx=variants.crypto_index(ser); ix=cx or variants.biquote_index(ser)
    if not ix: continue
    close=float(w['close_ts']); opened=close-900; ts,vs,g,P=grid(ix); K=float(w['strike'])
    if not cx:
        i0=bisect.bisect_left(ts,opened)
        if i0>=len(ts) or ts[i0]-opened>20: continue
        K=vs[i0]
    q=[(float(a),float(b),float(c)) for a,b,c in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],opened-20,close)).fetchall()]
    if len(q)<20: continue
    qt=[x[0] for x in q]; wid+=1; coin=variants.coin_of(ser)
    ko=int(opened)-G0; kstart=ko-3700
    if kstart<0 or int(close)-G0>=len(g): continue
    X=[0]*(int(close)-G0-kstart+2); prev=None
    for n,k in enumerate(range(kstart,int(close)-G0+1)):
        v=g[k]; sgn=1 if (v is not None and v>K) else 0
        X[n+1]=X[n]+(1 if (prev is not None and sgn!=prev) else 0); prev=sgn
    def cross(k,L): return X[k-kstart+1]-X[k-L-kstart+1]
    for e in range(60,841,30):
        at=opened+e; i=bisect.bisect_right(qt,at)-1
        if i<0 or at-qt[i]>20: continue
        _,yb,ya=q[i]; k=int(at)-G0
        if g[k] is None: continue
        left=close-at
        for side,bid,ask in (('yes',yb,ya),('no',1-ya,1-yb)):
            if not (0.1<=ask<=0.95): continue
            sg=1 if side=='yes' else -1; c=math.log(g[k]/K)*sg
            lets=''; zs=[]; ok=True
            for L in LOOKS:
                rate=math.sqrt((P[k]-P[k-L])/L)
                if rate<=0 or g[k-L] is None: ok=False; break
                z=c/(rate*math.sqrt(left)); slope=(c-math.log(g[k-L]/K)*sg)/(rate*math.sqrt(L))
                lets+=letter(z,slope,cross(k,L),THR[L]); zs.append(round(z,2))
            if not ok: continue
            won=w['result']==side; cc=10/ask; pnl=((1-ask)*cc if won else -10)-fee(ask)*cc
            rows.append((wid,'c' if cx else 'm',coin,e,side[0],round(ask,3),round(ask-bid,3),int(won),round(pnl,3),close,lets,zs))

FIRST_E = {"c": 120, "m": 600}
PB = lambda a: 0 if a < 0.35 else 1 if a < 0.5 else 2
rows = [r for r in rows if r[6] <= 0.04 and 0.20 <= r[5] <= 0.70 and r[3] >= FIRST_E[r[1]]]
print(len(rows), "rows; windows", wid)
day = lambda r: int(r[9] // 86400)
days = sorted({day(r) for r in rows})
G = lambda L: "u" if L in "AF" else "f" if L in "BE" else "d"  # the cushion rising, flat or falling on the bet's own side
KEYS = {"5m+60m": lambda l: l[2] + l[4], "5m+15m+60m directions": lambda l: G(l[2]) + G(l[3]) + G(l[4]), "5m+60m directions": lambda l: G(l[2]) + G(l[4])}
K, MIN_N = 10.0, 15
by_day = collections.defaultdict(list)
for r in rows:
    by_day[day(r)].append(r)


def record(keyf, train_days):
    seen, st_ = set(), collections.defaultdict(lambda: [0, 0.0])
    for d in train_days:
        for r in sorted(by_day[d], key=lambda x: x[3]):
            k = (r[0], r[4], keyf(r[10]), PB(r[5]))
            if k in seen:
                continue
            seen.add(k)
            c = st_[(keyf(r[10]), PB(r[5]))]
            c[0] += 1
            c[1] += r[8] / 10.0
    return st_


def test(keyf, thr, fixed=False):
    out = []
    for i, d in enumerate(days):
        if i < 3:
            continue
        st_ = None if fixed else record(keyf, days[:i])
        bought = set()
        for r in sorted(by_day[d], key=lambda x: x[3]):
            mk = (r[0], r[4])
            if mk in bought:
                continue
            if fixed:
                ok = r[10][4] == "D" and r[10][2] == "F"
            else:
                n, s_ = st_.get((keyf(r[10]), PB(r[5])), (0, 0.0))
                ok = n >= MIN_N and s_ / (n + K) >= thr
            if ok:
                bought.add(mk)
                out.append((d, r))
    return out


def show(name, out):
    if not out:
        print(f"{name:40s} no buys")
        return
    n = len(out)
    pn = sum(r[8] for _, r in out) / n
    half = sorted(d for d, _ in out)[n // 2]
    a = [r[8] for d, r in out if d < half]
    b = [r[8] for d, r in out if d >= half]
    cl = {c: [r[8] for _, r in out if r[1] == c] for c in "cm"}
    print(
        f"{name:40s} buys {n:4d} won {100 * sum(r[7] for _, r in out) / n:3.0f}% vs price {sum(r[5] for _, r in out) / n:.2f} net/$10 {pn:+.2f} | halves {sum(a) / max(len(a), 1):+.2f} (n={len(a)}) {sum(b) / max(len(b), 1):+.2f} (n={len(b)}) | crypto {sum(cl['c']) / max(len(cl['c']), 1):+.2f} (n={len(cl['c'])}) commodity {sum(cl['m']) / max(len(cl['m']), 1):+.2f} (n={len(cl['m'])})"
    )


print("test days", days[3:])
show("fixed 60m D + 5m F", test(None, 0, fixed=True))
for name, kf in KEYS.items():
    for thr in (0.0, 0.05, 0.10, 0.20):
        show(f"state record {name}, record >= {thr:.2f}", test(kf, thr))
print("DONE")
