"""Scalp exits for the wild-regime underdog (2026-11-06, user: "buy a low price, it usually comes back up a little: cash out when ahead or cut the loss"). Every 15-minute crypto market where a favorite of 0.70+ gave the underdog a price of 5-30c (no volatility filter, user: a scalp does not need it; `wild` as the first argument keeps only 1.3x+ volatility), buying the underdog at the flip price, then the underdog's bid path to the close: take at +X over the buy, stop at -Y, otherwise hold to the result. Net per $1 staked after the taker fee on the buy and on a sale; both halves."""
import os, time, calendar, statistics, math
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); now=time.time(); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,10,26,5,0,0))
recs=[r for r in variants.favorites(s,t0,now-600) if r['cls']=='crypto']
# the wild filter: coin volatility over its median, as in the other wild scripts
import sys
WILD=len(sys.argv)>1 and sys.argv[1]=='wild'
sel=[r for r in recs if r['ask']>=0.70 and 0.05<=r['flip_price']<=0.30 and (not WILD or (r.get('vol_rel') or 0)>=1.3)]
print('setups (all volatility)' if not WILD else 'setups (wild only)',len(sel))
paths=[]
for r in sel:
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts",(r['ticker'],r['ts']+2,r['close_ts'])).fetchall()
    if len(q)<5: continue
    # underdog bid path: the favorite is r['side']; the underdog is the other side
    ub=[(float(x[0]), (1.0-float(x[2])) if r['side']=='yes' else float(x[1])) for x in q]
    paths.append((r,ub))
print('with a quote path',len(paths))
def run(X,Y):
    out=[]
    for r,ub in paths:
        e=r['flip_price']; c=1.0/e; res=None
        for t,b in ub:
            if X and b>=e+X: res=(b-e)*c-(fee(e)+fee(b))*c; break
            if Y and b<=e-Y: res=(b-e)*c-(fee(e)+fee(max(b,.01)))*c; break
        if res is None:
            win=not r['won']; res=((1-e)*c if win else -e*c)-fee(e)*c
        out.append((r['close_ts'],res))
    return out
def line(name,X,Y):
    o=run(X,Y); n=len(o); o.sort(); a,b=o[:n//2],o[n//2:]
    f=lambda z: sum(v for _,v in z)/len(z)
    print(f"  {name:26} n={n} net/$1 {sum(v for _,v in o)/n:+.3f} | 1st half {f(a):+.3f}  2nd half {f(b):+.3f}")
line('hold to result',0,0)
for X in (0.03,0.05,0.08,0.12,0.20):
    for Y in (0,0.04,0.07,0.10):
        line(f'take +{X:.2f} stop -{Y:.2f}' if Y else f'take +{X:.2f} no stop',X,Y)
print('DONE')
