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
# ---- walk-forward replay of the curve model (appended to the helper definitions of hand_entry_study.py) ----
# Every minute 2-12 of every 15-minute market you hand-traded, both sides, ask 0.45-0.75 (the zone you buy in). Entry = the own-side ask at the close of the previous minute. Exit, conservative: the first minute whose CLOSE
# bid is >= ask+0.10 (sell there), else the close bid two minutes later; optimistic: sell at ask+0.10 if any of the next three minutes' bid HIGH reached it. Both net of the taker fee on each leg, per $10 ticket.
# The model is a logistic on features z-scored within each series (train-period stats), no spread, label = conservative net > 0. Train on earlier markets, test on later ones, three expanding blocks; top third of train scores
# is the buy rule. Compared with buying every moment in the band. First selected moment per market and side shown too (how a lane would buy).
import random
from kalshi_moonshot.strategy import fee_per_contract as fee
MON={m:i for i,m in enumerate('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split(),1)}
def tkey(tk):
    seg=tk.split('-')[1]; return (int(seg[0:2]),MON[seg[2:5]],int(seg[5:7]),int(seg[7:9]))
FE=['level','d1','d3','d5','rng','pos','swings','since_ext','from_hi','from_lo','vol1','elapsed']
tickers={}
for t in man: tickers[t.ticker]=t
print('markets',len(tickers),flush=True)
M=[]
for n,(tk,t) in enumerate(sorted(tickers.items(),key=lambda kv:tkey(kv[0]))):
    cs=candles(t)
    if not cs: continue
    fc=fill(cs)
    for side in ('yes','no'):
        for m in range(2,13):
            f=feats(side,fc,cs,m)
            if not f or not (0.45<=f['ask']<=0.75): continue
            ask=f['ask']; need=ask+0.10; opt=False; cons=None; last=None
            for j in range(m,min(m+3,15)):
                k=cs[j]; p=fc[j]
                if k is None or p is None: continue
                bhi=(k['bh'] if side=='yes' else (1-k['al'] if k['al'] is not None else None))
                if bhi is not None and bhi>=need: opt=True
                bc=own(side,p)[0]; last=bc
                if cons is None and bc>=need: cons=bc; break
            if last is None: continue
            ex=cons if cons is not None else last
            net=((ex-ask)-fee(ask)-fee(max(ex,0.01)))/ask*10
            ex_o=need if opt else ex
            net_o=((ex_o-ask)-fee(ask)-fee(max(ex_o,0.01)))/ask*10
            M.append(dict(series=t.series,ticker=tk,side=side,m=m,k=tkey(tk),net=net,net_o=net_o,hit=cons is not None,**f))
    if n%150==0: print('markets done',n,'moments',len(M),flush=True)
print('moments',len(M),'markets',len({x['ticker'] for x in M}))
M.sort(key=lambda x:(x['k'],x['m']))
def stats(train):
    by=collections.defaultdict(list)
    for x in train: by[x['series']].append(x)
    allx=train
    out={}
    for f in FE:
        g=(S.mean(x[f] for x in allx),S.pstdev([x[f] for x in allx]) or 1.0); out[('*',f)]=g
        for s_,L in by.items():
            if len(L)>=60: out[(s_,f)]=(S.mean(x[f] for x in L),S.pstdev([x[f] for x in L]) or 1.0)
    return out
def zed(x,st): return [(x[f]-st.get((x['series'],f),st[('*',f)])[0])/st.get((x['series'],f),st[('*',f)])[1] for f in FE]
def fit(Z,y,iters=200,lr=0.3,l2=0.01):
    w=[0.0]*(len(FE)+1); n=len(Z)
    for _ in range(iters):
        g=[0.0]*len(w)
        for z,t in zip(Z,y):
            s_=w[0]+sum(a*b for a,b in zip(w[1:],z)); p=1/(1+math.exp(-max(-30,min(30,s_)))); e=p-t
            g[0]+=e
            for j,v in enumerate(z): g[j+1]+=e*v
        w=[a-lr*(b/n+l2*a) for a,b in zip(w,g)]
    return w
sc=lambda w,z: w[0]+sum(a*b for a,b in zip(w[1:],z))
mk=sorted({x['ticker'] for x in M},key=tkey); N=len(mk)
res=[]
for bi,(a,b,c) in enumerate(((0.4,0.6,0.4),(0.6,0.8,0.6),(0.8,1.0,0.8))):
    trm=set(mk[:int(N*a)]); tem=set(mk[int(N*a):int(N*b)]) if b<1.0 else set(mk[int(N*a):])
    tr=[x for x in M if x['ticker'] in trm]; te=[x for x in M if x['ticker'] in tem]
    stt=stats(tr); w=fit([zed(x,stt) for x in tr],[1 if x['net']>0 else 0 for x in tr])
    cut=sorted((sc(w,zed(x,stt)) for x in tr),reverse=True)[len(tr)//3]
    for x in te: x['sc']=sc(w,zed(x,stt)); x['sel']=x['sc']>=cut; x['blk']=bi
    res+=te
    print(f"block {bi+1}: train {len(tr)} moments / test {len(te)} moments, markets {len(tem)}",flush=True)
def line(name,L):
    if len(L)<20: print(f'  {name:46} n{len(L)}'); return
    print(f"  {name:46} n{len(L):5d}  win {100*S.mean(1 if x['net']>0 else 0 for x in L):3.0f}%  net/$10 conservative {S.mean(x['net'] for x in L):+5.2f}  optimistic {S.mean(x['net_o'] for x in L):+5.2f}")
def first(L):
    seen=set(); out=[]
    for x in sorted(L,key=lambda x:(x['k'],x['ticker'],x['side'],x['m'])):
        key=(x['ticker'],x['side'])
        if key in seen: continue
        seen.add(key); out.append(x)
    return out
for blk in (0,1,2,None):
    L=[x for x in res if blk is None or x['blk']==blk]
    print(f"\n{'ALL TEST BLOCKS' if blk is None else f'test block {blk+1}'} (later markets than the training days)")
    line('every moment in the band',L); line('top-third model score',[x for x in L if x['sel']]); line('the rest',[x for x in L if not x['sel']])
    line('first selected moment per market+side',first([x for x in L if x['sel']]))
print('\nBY SERIES (all test blocks): top-third vs every moment')
for s_ in ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M','KXGOLD15M','KXSILVER15M','KXCOPPER15M','KXWTI15M'):
    L=[x for x in res if x['series']==s_]
    if len(L)>=60: line(f'{s_} every moment',L); line(f'{s_} top third',[x for x in L if x['sel']])
print('DONE')
