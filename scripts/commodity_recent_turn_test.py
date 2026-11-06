"""Does a turn in the last 1-5 minutes before a commodity buy separate winners from losers (user, silver 10:15: a V, down then up, and the bot bought the down side)? Every settled live commodity bet since 10-28 (excluding multiscale), the bet side's spot drift over the last L seconds up to the buy, in typical minutes of that market's own last 15 minutes. With us / flat / against us, both halves; also the V: the 7-minute drift before the last 3 minutes with us but the last 3 minutes against."""
import calendar, math, bisect
from kalshi_moonshot import config, db, biquotefeed
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,'tape',shared=True)
t0=calendar.timegm((2026,10,28,0,0,0))
rows=st.db.execute("SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price,exit_reason FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND lane LIKE 'commodity_%' AND lane<>'commodity_multiscale' AND (outcome IS NOT NULL OR exit_price IS NOT NULL) ORDER BY ts",(t0,)).fetchall()
seen={}
for r in rows: seen[(r['ticker'],r['lane'],r['side'],round(r['ask'],3))]=r
LB=(60,120,180,300)
out=[]
for r in seen.values():
    idx=biquotefeed.index_for_series(r['ticker'].split('-')[0])
    if not idx: continue
    tk=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts",(idx,r['ts']-1200,r['ts'])).fetchall()
    if len(tk)<500: continue
    T=[x['ts'] for x in tk]; V=[x['value'] for x in tk]
    d=[math.log(b/a) for a,b in zip(V,V[1:])]
    unit=math.sqrt(sum(x*x for x in d)/len(d))*math.sqrt(60)
    if unit<=0: continue
    def val(t):
        i=bisect.bisect_right(T,t)-1
        return V[i] if i>=0 else None
    now=V[-1]; sg=1 if r['side']=='yes' else -1
    tr={}
    ok=True
    for L in LB:
        b=val(r['ts']-L)
        if b is None: ok=False;break
        tr[L]=sg*math.log(now/b)/unit
    a3=val(r['ts']-180); b10=val(r['ts']-180-420)
    if not ok or a3 is None or b10 is None: continue
    tr['pre']=sg*math.log(a3/b10)/unit
    c=r['stake_usd']/r['ask']
    if r['exit_price'] is not None: p=(r['exit_price']-r['ask'])*c-(fee(r['ask'])+fee(max(r['exit_price'],.01)))*c
    else: p=((1-r['ask'])*c if r['outcome']==r['side'] else -r['ask']*c)-fee(r['ask'])*c
    out.append(dict(ts=r['ts'],tr=tr,p=p,stk=r['stake_usd'],total=(r['exit_price'] is None and r['outcome']!=r['side'])))
print('bets',len(out))
def row(name,v):
    if v: print(f"  {name:30} n={len(v):4d} net ${sum(o['p'] for o in v):+8.2f} on ${sum(o['stk'] for o in v):6.0f} {100*sum(o['p'] for o in v)/sum(o['stk'] for o in v):+5.1f}%  total losses {sum(1 for o in v if o['total'])}")
cut=sorted(o['ts'] for o in out)[len(out)//2]
for L in LB:
    print(f'last {L}s up to the buy:')
    for nm,f in (('ALL',lambda o:True),('1st half',lambda o:o['ts']<cut),('2nd half',lambda o:o['ts']>=cut)):
        v=[o for o in out if f(o)]
        row(f'{nm} with us (>+0.5)',[o for o in v if o['tr'][L]>0.5]); row(f'{nm} flat',[o for o in v if -0.5<=o['tr'][L]<=0.5]); row(f'{nm} against (<-0.5)',[o for o in v if o['tr'][L]<-0.5])
print('the V: the 7 min before the last 3 min with us, the last 3 min against us:')
for nm,f in (('ALL',lambda o:True),('1st half',lambda o:o['ts']<cut),('2nd half',lambda o:o['ts']>=cut)):
    v=[o for o in out if f(o)]
    row(f'{nm} V (pre>+0.5, 3m<-0.5)',[o for o in v if o['tr']['pre']>0.5 and o['tr'][180]<-0.5])
    row(f'{nm} not V',[o for o in v if not (o['tr']['pre']>0.5 and o['tr'][180]<-0.5)])
print('DONE')
