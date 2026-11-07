"""Scalp states: which curve states let a resting sell above the price paid fill, and does that beat holding to the result?

Question (2026-11-07, user: most hand-placed wins were scalps read from the curve): on the moments the multiscale scan covers (every 30 s, both sides, ask 0.20-0.70, spread <= 0.04, minute 2 onward), for each state (the varieties at 5m+60m, and at 1m+5m+60m) and price band, how often did the held side's bid reach the price paid plus a target in the time before 20 s to the close, and what is the net of "sell at the target when it prints, else hold to the result" against holding? The buy pays the taker fee; the resting sell pays none. Targets: 0.05, 0.10 and 0.20 over the price paid. States are curve readings, never a coin or an asset class; the class is a label only.
Walk-forward as scripts/state_record_walkforward.py: the record of a state is every earlier day; the later days buy the states whose shrunk scalp gain over holding clears THR. Two halves of the test days and the class split are printed.
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
    sufy=[0.0]*(len(q)+1); sufn=[0.0]*(len(q)+1)
    for i_ in range(len(q)-1,-1,-1):
        ok_=q[i_][0]<=close-20
        sufy[i_]=max(sufy[i_+1],q[i_][1] if ok_ else 0.0); sufn[i_]=max(sufn[i_+1],(1-q[i_][2]) if ok_ else 0.0)
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
            j_=bisect.bisect_right(qt,at+3)
            mx=(sufy if side=='yes' else sufn)[j_]
            rows.append((wid,'c' if cx else 'm',coin,e,side[0],round(ask,3),round(ask-bid,3),int(won),round(pnl,3),close,lets,zs,round(mx,3)))

TARGETS = (0.05, 0.10, 0.20)
FIRST_E = 120
PB = lambda a: 0 if a < 0.35 else 1 if a < 0.5 else 2
rows = [r for r in rows if r[6] <= 0.04 and 0.20 <= r[5] <= 0.70 and r[3] >= FIRST_E]
print(len(rows), "rows; windows", wid)
day = lambda r: int(r[9] // 86400)
days = sorted({day(r) for r in rows})
KEYS = {"5m+60m": lambda l: l[2] + l[4], "1m+5m+60m": lambda l: l[0] + l[2] + l[4]}


def scalp_net(r, tgt):
    """Per $1 staked: sell at ask + tgt if the bid printed it, else hold to the result; the buy's taker fee in."""
    ask, mx = r[5], r[12]
    c = 1.0 / ask
    fee_ = fee(ask) * c
    if mx >= ask + tgt - 1e-9:
        return tgt * c - fee_
    return ((1 - ask) * c if r[7] else -1.0) - fee_


def hold_net(r):
    return r[8] / 10.0


K, MIN_N = 10.0, 15
by_day = collections.defaultdict(list)
for r in rows:
    by_day[day(r)].append(r)


def record(keyf, tgt, train_days):
    seen, st_ = set(), collections.defaultdict(lambda: [0, 0.0, 0.0])
    for d in train_days:
        for r in sorted(by_day[d], key=lambda x: x[3]):
            k = (r[0], r[4], keyf(r[10]), PB(r[5]))
            if k in seen:
                continue
            seen.add(k)
            c = st_[(keyf(r[10]), PB(r[5]))]
            c[0] += 1
            c[1] += scalp_net(r, tgt) - hold_net(r)  # the gain of scalping over holding
            c[2] += scalp_net(r, tgt)
    return st_


def test(keyf, tgt, thr):
    out = []
    for i, d in enumerate(days):
        if i < 3:
            continue
        st_ = record(keyf, tgt, days[:i])
        bought = set()
        for r in sorted(by_day[d], key=lambda x: x[3]):
            mk = (r[0], r[4])
            if mk in bought:
                continue
            n, gain, _tot = st_.get((keyf(r[10]), PB(r[5])), (0, 0.0, 0.0))
            if n >= MIN_N and gain / (n + K) >= thr:
                bought.add(mk)
                out.append((d, r))
    return out


def show(name, out, tgt):
    if not out:
        print(f"{name:46s} no buys")
        return
    n = len(out)
    sc = [scalp_net(r, tgt) * 10 for _, r in out]
    hd = [hold_net(r) * 10 for _, r in out]
    half = sorted(d for d, _ in out)[n // 2]
    a = [x for (d, _), x in zip(out, sc) if d < half]
    b = [x for (d, _), x in zip(out, sc) if d >= half]
    fills = sum(1 for _, r in out if r[12] >= r[5] + tgt - 1e-9)
    cl = {c: [x for (_, r), x in zip(out, sc) if r[1] == c] for c in "cm"}
    print(f"{name:46s} buys {n:4d} sell filled {100 * fills / n:3.0f}% scalp net/$10 {sum(sc) / n:+.2f} (hold the same buys {sum(hd) / n:+.2f}) | halves {sum(a) / max(len(a), 1):+.2f} (n={len(a)}) {sum(b) / max(len(b), 1):+.2f} (n={len(b)}) | crypto {sum(cl['c']) / max(len(cl['c']), 1):+.2f} (n={len(cl['c'])}) commodity {sum(cl['m']) / max(len(cl['m']), 1):+.2f} (n={len(cl['m'])})")


print("test days", days[3:])
for tgt in TARGETS:
    allr = {}
    for r in rows:
        allr.setdefault((r[0], r[4]), r)
    ar = list(allr.values())
    print(f"target +{tgt:.2f}: every market side at its first moment, scalp net/$10 {sum(scalp_net(r, tgt) for r in ar) * 10 / len(ar):+.2f} vs hold {sum(hold_net(r) for r in ar) * 10 / len(ar):+.2f} (n={len(ar)}), sell filled {100 * sum(1 for r in ar if r[12] >= r[5] + tgt - 1e-9) / len(ar):.0f}%")
for name, kf in KEYS.items():
    for tgt in TARGETS:
        for thr in (0.05, 0.10):
            show(f"scalp record {name} +{tgt:.2f}, gain >= {thr:.2f}", test(kf, tgt, thr), tgt)
print("DONE")
