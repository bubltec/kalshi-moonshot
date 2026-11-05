import json, math, time, itertools, statistics as S, datetime, urllib.request, urllib.parse, collections
from kalshi_moonshot import config, trips
from kalshi_moonshot.store import Store
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions)
c=KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
PROD={'KXBTC15M':'BTC-USD','KXETH15M':'ETH-USD','KXSOL15M':'SOL-USD','KXXRP15M':'XRP-USD','KXDOGE15M':'DOGE-USD'}
B=json.load(open('/tmp/spotbars.json'))
def fetch(prod,a,b):
    q=urllib.parse.urlencode({'start':datetime.datetime.fromtimestamp(a,datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),'end':datetime.datetime.fromtimestamp(b,datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),'granularity':60})
    req=urllib.request.Request(f'https://api.exchange.coinbase.com/products/{prod}/candles?{q}',headers={'User-Agent':'moonshot-research'})
    with urllib.request.urlopen(req,timeout=20) as r: return json.loads(r.read())
res={}
def result_of(tk):
    if tk in res: return res[tk]
    r=st.db.execute('SELECT result FROM windows WHERE ticker=?',(tk,)).fetchone()
    v=r['result'] if r and r['result'] in ('yes','no') else None
    if v is None:
        try:
            m=c.market(tk); m=m.get('market',m); v=m.get('result') if m.get('result') in ('yes','no') else None
        except Exception: v=None
        time.sleep(0.08)
    res[tk]=v; return v
allt=trips.build_trips(st)
L=[]; need=[]
for t in allt:
    if t.source!='manual' or t.series not in PROD or not t.done or not t.reliable or not t.close_ts: continue
    if t.cashed_out<=0 or t.settled>0 or t.exit_contracts<=0 or not t.closed_ts: continue
    rr=result_of(t.ticker)
    if rr is None: continue
    sale=t.exit_value/t.exit_contracts
    L.append(dict(t=t,res=rr,sale=sale,left=t.close_ts-t.closed_ts))
    if t.ticker not in B: need.append(t)
print('crypto cash-outs',len(L),'; spot bars missing for',len({t.ticker for t in need}),flush=True)
for t in {t.ticker:t for t in need}.values():
    for a in range(3):
        try:
            x=fetch(PROD[t.series],t.close_ts-900-3700,t.close_ts); break
        except Exception: time.sleep(1.5*(a+1)); x=None
    if x: B[t.ticker]=sorted([[k[0],k[1],k[2],k[3],k[4]] for k in x])
    time.sleep(0.12)
def feats(r):
    t=r['t']; bars=B.get(t.ticker)
    if not bars: return None
    o=t.close_ts-900; pre=[b for b in bars if b[0]+60<=o]
    if len(pre)<3: return None
    kb=pre[-1]; K=(kb[1]+kb[2]+kb[3]+kb[4])/4.0
    sig_src=[b[4] for b in bars if b[0]+60<=o][-61:]
    rets=[math.log(b/a) for a,b in itertools.pairwise(sig_src)]
    sig=S.pstdev(rets) if len(rets)>10 else None
    if not sig: return None
    until=t.closed_ts
    seg=[b[4] for b in bars if o<=b[0] and b[0]+60<=until]
    if len(seg)<2: return None
    sg=[1 if x>K else 0 for x in seg]; cross=sum(1 for a,b in itertools.pairwise(sg) if a!=b)
    side=1.0 if t.side=='yes' else -1.0
    left=max((t.close_ts-until)/60.0,0.25)
    z=math.log(seg[-1]/K)*side/(sig*math.sqrt(left))
    return dict(cross=cross,z=z)
M=[]
for r in L:
    f=feats(r)
    if f: M.append(dict(r,**f))
print('with spot data:',len(M),flush=True)
def row(label,X):
    if len(X)<20: print(f"   {label:46} n={len(X)} n/a"); return
    hold=[1.0 if r['res']==r['t'].side else 0.0 for r in X]; sell=[r['sale']-fee(max(r['sale'],0.01)) for r in X]
    print(f"   {label:46} n={len(X):3d} won in the end {100*S.mean(hold):3.0f}%  sold for {100*S.mean(sell):5.1f}c vs holding {100*S.mean(hold):5.1f}c => holding {100*(S.mean(hold)-S.mean(sell)):+5.1f}c a contract")
gain=[r for r in M if r['sale']-r['t'].entry_price>0.02]
print('\nSOLD AT A GAIN (crypto), by how often the spot had crossed the target before you sold (minute closes since the open)')
row('all gains',gain)
for lo,hi,lab in ((0,1,'0 crossings'),(1,3,'1-2 crossings'),(3,99,'3+ crossings')):
    row(lab,[r for r in gain if lo<=r['cross']<hi])
print('   and by the sale price:')
for lo,hi in ((0.5,0.8),(0.8,1.01)):
    for a,b,lab in ((0,1,'0 crossings'),(1,3,'1-2 crossings'),(3,99,'3+ crossings')):
        row(f"sold {lo:.2f}-{hi:.2f}, {lab}",[r for r in gain if lo<=r['sale']<hi and a<=r['cross']<b])
print('\nSOLD AT A GAIN, by how far the spot was beyond the target in your favor when you sold (cushion z)')
for lo,hi,lab in ((-9,0,'spot on the wrong side'),(0,1,'cushion 0-1'),(1,2,'cushion 1-2'),(2,99,'cushion over 2')):
    row(lab,[r for r in gain if lo<=r['z']<hi])
print('\nBY TIME LEFT AND CROSSINGS (sold at a gain)')
for a,b,lab in ((0,180,'under 3 minutes left'),(180,900,'over 3 minutes left')):
    for lo,hi,cl in ((0,2,'0-1 crossings'),(2,99,'2+ crossings')):
        row(f"{lab}, {cl}",[r for r in gain if a<=r['left']<b and lo<=r['cross']<hi])
print('DONE')
