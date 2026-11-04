import os, time, bisect, statistics, collections
os.nice(15)
from kalshi_moonshot import config, db
from kalshi_moonshot.variants import favorite_entry
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
W=st.db.execute("SELECT ticker,series,close_ts,result FROM windows WHERE series LIKE 'KX%YRRATE15M' AND result IN ('yes','no') ORDER BY close_ts").fetchall()
favs=[]
for w in W:
    close=float(w['close_ts'])
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],close-900,close+5)).fetchall()
    if len(q)<30: continue
    qq=[(float(a),float(b),float(c)) for a,b,c in q]
    pk=favorite_entry([x for x in qq if 120<=close-x[0]<=480],close)
    if not pk: continue
    side,ask,at,bid=pk; ts=[x[0] for x in qq]
    def midat(t):
        k=bisect.bisect_right(ts,t)-1
        return None if k<0 or t-ts[k]>20 else (qq[k][1]+qq[k][2])/2
    g=[x for x in (midat(at-300+5*i) for i in range(61)) if x is not None]
    if len(g)<40: continue
    cross=sum(1 for a,b in zip(g,g[1:]) if (a-0.5)*(b-0.5)<0)
    path=[(x[0],(x[1] if side=='yes' else 1-x[2])) for x in qq if x[0]>at and x[0]<=close-5]  # (time, our side's bid)
    favs.append(dict(ask=ask,at=at,close=close,side=side,won=w['result']==side,cross=cross,path=path))
print('favorites',len(favs),'calm',sum(1 for f in favs if f['cross']==0))
def sim(f,thr,secs,last=None):
    """Sell at the first moment our side's bid has stayed at or under thr for secs seconds in a row (and, if last is set, only in the final `last` seconds)."""
    ask=f['ask']; c=10/ask; since=None
    for t,b in f['path']:
        if b<=thr and (last is None or f['close']-t<=last):
            since=t if since is None else since
            if t-since>=secs: return c*(b-ask)-fee(ask)*c-fee(max(b,.01))*c, True
        else: since=None
    r=(c*(1-ask) if f['won'] else -10)-fee(ask)*c
    return r, False
for label,subset in (('ALL',favs),('CALM only',[f for f in favs if f['cross']==0])):
    base=sum(sim(f,-1,0)[0] for f in subset)
    print('==',label,len(subset),'hold',round(base,1))
    for thr in (0.40,0.50):
        for secs in (0,15,30,60):
            for last in (None,180):
                tot=0;sold=0;w=0;l=0
                for f in subset:
                    p,did=sim(f,thr,secs,last); tot+=p
                    if did:
                        sold+=1
                        if f['won']: w+=1
                        else: l+=1
                print(f"  bid<={thr:.2f} for {secs:2d}s {'any time' if last is None else 'last 3 min':10} net {tot:+7.1f} vs hold {tot-base:+7.1f}  sold {sold:3d} (winners {w}, losers {l})")
print('DONE')
