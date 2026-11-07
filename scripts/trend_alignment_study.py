"""Trend alignment: does buying the side that all of the 5-minute, 15-minute and 60-minute curves are moving toward pay, against the contrarian state the record buys (60m approaching, 5m pulling away)?

Question (2026-11-07, user: an XRP bet on the down side while the hour, 15-minute and 5-minute curves all rose): on every moment the multiscale scan covers (every 30 s, both sides, ask 0.20-0.70, spread <= 0.04, minute 2 onward), group the bet's side by the direction of its cushion at 5m, 15m and 60m (up = letters A or F, flat = B or E, down = C or D: the cushion is the distance from the strike on the bet's own side), one reading per market side, group and price band, held to the result, taker fee in. Reports n, win rate against the price paid, net per $10 ticket and the two halves; the class is a label only.
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

G = lambda L: "u" if L in "AF" else "f" if L in "BE" else "d"
rows = [r for r in rows if r[6] <= 0.04 and 0.20 <= r[5] <= 0.70 and r[3] >= 120]
day = lambda r: int(r[9] // 86400)
mid = sorted(r[9] for r in rows)[len(rows) // 2]
seen, grp = set(), collections.defaultdict(list)
PB = lambda a: 0 if a < 0.35 else 1 if a < 0.5 else 2
for r in sorted(rows, key=lambda x: x[3]):
    g = G(r[10][2]) + G(r[10][3]) + G(r[10][4])  # 5m, 15m, 60m
    k = (r[0], r[4], g, PB(r[5]))
    if k in seen:
        continue
    seen.add(k)
    grp[(g, PB(r[5]))].append(r)
print("rows", len(rows), "| groups are the cushion direction at 5m,15m,60m (u up, f flat, d down) for the bet's own side")
print("group  band     n  won%  avg price  net/$10   first half / second half   crypto / commodity")
def line(name, rs):
    n = len(rs)
    h1 = [r[8] for r in rs if r[9] <= mid]; h2 = [r[8] for r in rs if r[9] > mid]
    c = [r[8] for r in rs if r[1] == "c"]; m = [r[8] for r in rs if r[1] == "m"]
    print(f"{name:11s} {n:5d} {100 * sum(r[7] for r in rs) / n:4.0f}% {sum(r[5] for r in rs) / n:6.2f} {sum(r[8] for r in rs) / n:+8.2f}   {sum(h1) / max(len(h1), 1):+6.2f} ({len(h1)}) / {sum(h2) / max(len(h2), 1):+6.2f} ({len(h2)})   {sum(c) / max(len(c), 1):+6.2f} ({len(c)}) / {sum(m) / max(len(m), 1):+6.2f} ({len(m)})")
for key in ("uuu", "ufu", "uud", "udu", "dud", "duu", "ddu", "dduu"[:3], "ddd", "uuf", "uff", "fff"):
    pass
for g in sorted({k[0] for k in grp}):
    for b in (0, 1, 2):
        rs = grp.get((g, b), [])
        if len(rs) >= 40:
            line(f"{g} band{b}", rs)
print("--- all three up (uuu) vs the record's contrarian (5m up, 60m down: u?d), any 15m")
line("uuu all", [r for (g, b), rs in grp.items() if g == "uuu" for r in rs])
line("u?d all", [r for (g, b), rs in grp.items() if g[0] == "u" and g[2] == "d" for r in rs])
line("ddd all", [r for (g, b), rs in grp.items() if g == "ddd" for r in rs])
print("DONE")
