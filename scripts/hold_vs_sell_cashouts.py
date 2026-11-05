import math, statistics as S, collections
from kalshi_moonshot import config, trips
from kalshi_moonshot.store import Store
from kalshi_moonshot.kalshi import KalshiClient
import time
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions)
allt=trips.build_trips(st)
c=KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
L=[]
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
for t in allt:
    if t.source!='manual' or not t.series.endswith('15M') or not t.done or not t.reliable or not t.close_ts: continue
    if t.cashed_out<=0 or t.settled>0 or t.exit_contracts<=0 or not t.closed_ts: continue
    rr=result_of(t.ticker)
    if rr is None: continue
    sale=t.exit_value/t.exit_contracts; entry=t.entry_price
    left=t.close_ts-t.closed_ts
    if left<0 or left>900: continue
    hold=1.0 if rr==t.side else 0.0
    L.append(dict(sale=sale,entry=entry,left=left,hold=hold,gain=sale-entry,series=t.series,net_sell=sale-fee(max(sale,0.01)),cont=t.exit_contracts))
print(len(L),'of your 15-minute trips were fully cashed out before the close (manual)')
gain=[r for r in L if r['gain']>0.02]
print(len(gain),'were sold at a gain of more than 2c; total contracts',round(sum(r['cont'] for r in gain)))
def row(label,X):
    if len(X)<25: print(f"   {label:34} n={len(X)} n/a"); return
    w=sum(r['hold'] for r in X)/len(X)
    sell=S.mean(r['net_sell'] for r in X); hold=S.mean(r['hold'] for r in X)
    d=S.mean(r['hold']-r['net_sell'] for r in X)
    print(f"   {label:34} n={len(X):4d}  it won in the end {100*w:4.0f}%  you got {100*sell:5.1f}c (after the sale fee) vs holding {100*hold:5.1f}c  => holding {100*d:+5.1f}c a contract")
print('\nSOLD AT A GAIN: what you got vs what holding to the close would have paid, by the price you sold at')
for lo,hi in ((0.5,0.7),(0.7,0.8),(0.8,0.9),(0.9,1.01)):
    row(f"sold at {lo:.2f}-{hi:.2f}",[r for r in gain if lo<=r['sale']<hi])
print('\nby how much time was left when you sold')
for lo,hi,lab in ((0,60,'under 1 minute left'),(60,120,'1-2 minutes left'),(120,300,'2-5 minutes left'),(300,900,'over 5 minutes left')):
    row(lab,[r for r in gain if lo<=r['left']<hi])
print('\nprice x time')
for lo,hi in ((0.7,0.85),(0.85,1.01)):
    for a,b,lab in ((0,120,'<2 min left'),(120,300,'2-5 min left'),(300,900,'>5 min left')):
        row(f"sold {lo:.2f}-{hi:.2f}, {lab}",[r for r in gain if lo<=r['sale']<hi and a<=r['left']<b])
print('\nSOLD AT A LOSS or flat (for contrast)')
loss=[r for r in L if r['gain']<=0.02]
row('sold at a loss or flat',loss)
for lo,hi in ((0.0,0.3),(0.3,0.5),(0.5,0.7),(0.7,1.01)):
    row(f"  sold at {lo:.2f}-{hi:.2f}",[r for r in loss if lo<=r['sale']<hi])
print('DONE')
