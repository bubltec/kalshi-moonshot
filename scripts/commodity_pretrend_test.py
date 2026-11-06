"""Does the trend before a spiky stretch (user: when it is spiky, look at the hour line) separate commodity winners from losers? Every settled live commodity bet since 10-28 (one per market, side and lane), the bet side's spot drift over a window ending 90 s before the buy (7 min and 60 min), in typical minutes of that market's own last 15 minutes; with-us vs rest, both halves. Also the shock exits: sold with the trend or against it, and what holding would have paid."""
import calendar, math, bisect, collections
from kalshi_moonshot import config, db, biquotefeed
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,'tape',shared=True)
t0=calendar.timegm((2026,10,28,0,0,0))
rows=st.db.execute("SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price,exit_reason FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND lane LIKE 'commodity_%' AND lane<>'commodity_multiscale' AND (outcome IS NOT NULL OR exit_price IS NOT NULL) ORDER BY ts",(t0,)).fetchall()
seen={}
for r in rows: seen[(r['ticker'],r['lane'],r['side'],round(r['ask'],3))]=r
cache={}
def ticks(idx,a,b):
    return tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts",(idx,a,b)).fetchall()
out=[]
for r in seen.values():
    idx=biquotefeed.index_for_series(r['ticker'].split('-')[0])
    if not idx: continue
    tk=ticks(idx,r['ts']-3900,r['ts'])
    if len(tk)<900: continue
    T=[x['ts'] for x in tk]; V=[x['value'] for x in tk]
    last=[(t,v) for t,v in zip(T,V) if t>r['ts']-900]
    d=[math.log(b[1]/a[1]) for a,b in zip(last,last[1:])]
    if len(d)<100: continue
    unit=math.sqrt(sum(x*x for x in d)/len(d))*math.sqrt(60)   # per sample; samples ~1 per second
    if unit<=0: continue
    def val(t):
        i=bisect.bisect_right(T,t)-1
        return V[i] if i>=0 else None
    a=val(r['ts']-90); b7=val(r['ts']-90-420); b60=val(r['ts']-90-3600+90)
    if a is None or b7 is None: continue
    sg=1 if r['side']=='yes' else -1
    tr7=sg*math.log(a/b7)/unit
    tr60=sg*math.log(a/b60)/unit/math.sqrt(8) if b60 else None
    c=r['stake_usd']/r['ask']
    hold=((1-r['ask'])*c if (r['outcome']==r['side']) else -r['ask']*c)-fee(r['ask'])*c if r['outcome'] else None
    if r['exit_price'] is not None: p=(r['exit_price']-r['ask'])*c-(fee(r['ask'])+fee(max(r['exit_price'],.01)))*c
    else: p=hold
    out.append(dict(ts=r['ts'],tr7=tr7,tr60=tr60,p=p,stk=r['stake_usd'],exit=r['exit_price'] is not None,reason=r['exit_reason'] or '',hold=hold,lane=r['lane']))
print('bets',len(out))
def row(name,v):
    if v: print(f"  {name:34} n={len(v):4d} net ${sum(o['p'] for o in v):+8.2f} on ${sum(o['stk'] for o in v):7.0f} {100*sum(o['p'] for o in v)/sum(o['stk'] for o in v):+5.1f}%")
cut=sorted(o['ts'] for o in out)[len(out)//2]
for key in ('tr7','tr60'):
    print(key,'(bet side drift before the last 90 s, in typical minutes)')
    for nm,f in (('ALL',lambda o:True),('1st half',lambda o:o['ts']<cut),('2nd half',lambda o:o['ts']>=cut)):
        v=[o for o in out if f(o) and o[key] is not None]
        row(f'{nm} with us (>+0.5)',[o for o in v if o[key]>0.5]); row(f'{nm} flat',[o for o in v if -0.5<=o[key]<=0.5]); row(f'{nm} against (<-0.5)',[o for o in v if o[key]<-0.5])
print('shock exits (sold) by the trend, and what holding would have paid:')
ex=[o for o in out if o['exit'] and 'shock' in o['reason'] and o['hold'] is not None]
for nm,f in (('with us',lambda o:o['tr7']>0.5),('flat',lambda o:-0.5<=o['tr7']<=0.5),('against',lambda o:o['tr7']<-0.5)):
    v=[o for o in ex if f(o)]
    if v: print(f"  shock sold {nm:8} n={len(v):3d} sold ${sum(o['p'] for o in v):+7.2f}  holding ${sum(o['hold'] for o in v):+7.2f}")
print('DONE')
