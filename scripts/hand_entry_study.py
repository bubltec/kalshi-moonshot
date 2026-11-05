import os, time, json, math, itertools, statistics as S, collections, datetime
os.nice(15)
from kalshi_moonshot import config, trips
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.store import Store
s=config.reload(); st=Store(s.db_path,s.exclude_versions)
c=KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
allt=trips.build_trips(st)
man=[t for t in allt if t.source=='manual' and t.series.endswith('15M') and t.done and t.reliable and t.close_ts and t.contracts>0]
print('manual 15-minute trips to study:',len(man),flush=True)
def num(d,k):
    try: return float(d[k])
    except Exception: return None
def candles(t):
    open_=t.close_ts-900
    for path in (f'/series/{t.series}/markets/{t.ticker}/candlesticks',):
        try:
            body=c._request('GET',path,params={'start_ts':int(open_),'end_ts':int(t.close_ts),'period_interval':1},auth=True)
        except Exception as e:
            return None
        cs=body.get('candlesticks') or []
        out=[None]*15
        for k in cs:
            idx=int(round((k['end_period_ts']-open_)/60.0))-1
            if 0<=idx<15:
                yb=k.get('yes_bid') or {}; ya=k.get('yes_ask') or {}
                out[idx]=dict(bb=num(yb,'close_dollars'),aa=num(ya,'close_dollars'),bh=num(yb,'high_dollars'),al=num(ya,'low_dollars'),v=num(k,'volume_fp') or 0.0)
        return out
def fill(cs):
    last=None; res=[]
    for k in cs:
        if k and k['bb'] is not None and k['aa'] is not None: last=k
        res.append(last)
    return res
def own(side,k):   # own-side bid, ask, mid at the close of a candle
    bb,aa=k['bb'],k['aa']
    return (bb,aa) if side=='yes' else (1-aa,1-bb)
def swings(q,th=0.08):
    n=0; ext=q[0]; d=0
    for x in q[1:]:
        if d>=0 and x>ext: ext=x; d=1 if d==0 and x-q[0]>=th else d
        elif d<=0 and x<ext: ext=x; d=-1 if d==0 and q[0]-x>=th else d
        if d==1 and ext-x>=th: n+=1; d=-1; ext=x
        elif d==-1 and x-ext>=th: n+=1; d=1; ext=x
    return n
def feats(side,fc,cs,m):
    # uses only completed minutes before m
    if m<2: return None
    prev=[fc[j] for j in range(m)]
    if any(p is None for p in prev): return None
    q=[(own(side,p)[0]+own(side,p)[1])/2 for p in prev]
    bid,ask=own(side,prev[-1])
    mx,mn=max(q),min(q); rng=mx-mn
    i_hi=len(q)-1-q[::-1].index(mx); i_lo=len(q)-1-q[::-1].index(mn)
    since=len(q)-1-max(i_hi,i_lo)
    vols=[(cs[j]['v'] if cs[j] else 0.0) for j in range(m)]
    mv=S.mean(vols) if S.mean(vols)>0 else 1.0
    return dict(level=q[-1],d1=q[-1]-q[-2],d3=q[-1]-q[max(0,len(q)-4)],d5=q[-1]-q[max(0,len(q)-6)],rng=rng,pos=(q[-1]-mn)/rng if rng>0.01 else 0.5,
                swings=swings(q),since_ext=since,from_hi=mx-q[-1],from_lo=q[-1]-mn,vol1=vols[-1]/mv,spread=ask-bid,elapsed=m,ask=ask)
def outcome(side,fc,cs,m,entry_ask):
    best=None
    for j in range(m,min(m+3,15)):
        k=cs[j] if cs[j] else None
        if not k or k['bh'] is None or k['al'] is None: continue
        b=k['bh'] if side=='yes' else 1-k['al']
        best=b if best is None else max(best,b)
    if best is None: return None
    return best-entry_ask
rows=[]; skipped=0
for n,t in enumerate(man):
    cs=candles(t)
    if not cs: skipped+=1; continue
    fc=fill(cs)
    open_=t.close_ts-900
    m=int((t.opened_ts-open_)//60)
    if not (2<=m<=12): skipped+=1; continue
    f=feats(t.side,fc,cs,m)
    if not f: skipped+=1; continue
    entry=t.entry_price
    o=outcome(t.side,fc,cs,m,entry)
    if o is None: skipped+=1; continue
    rows.append(dict(kind='user',series=t.series,ticker=t.ticker,side=t.side,m=m,entry=entry,out=o,pnl=t.pnl,roi=t.roi,**f))
    for mm in range(2,13):
        if abs(mm-m)<=1: continue
        fc2=feats(t.side,fc,cs,mm)
        if not fc2 or abs(fc2['ask']-entry)>0.15: continue
        o2=outcome(t.side,fc,cs,mm,fc2['ask'])
        if o2 is None: continue
        rows.append(dict(kind='ctrl',series=t.series,ticker=t.ticker,side=t.side,m=mm,entry=fc2['ask'],out=o2,pnl=None,roi=None,**fc2))
    if n%100==0: print('done',n,'rows',len(rows),flush=True)
json.dump(rows,open('/tmp/handstudy.json','w'))
print('trips used',sum(1 for r in rows if r['kind']=='user'),'skipped',skipped,'controls',sum(1 for r in rows if r['kind']=='ctrl'),'DONE',flush=True)
