import os, time, bisect, math, collections, statistics, calendar, json, gzip, base64
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.biquotefeed import index_for_series as bq
from kalshi_moonshot.kalshiws import index_for_series as cx
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); now=time.time(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,10,28,5,0,0))
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(t0,)).fetchall()
cache={}
def feed(ix):
    if ix not in cache:
        r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,t0-1500)).fetchall(); cache[ix]=([x[0] for x in r],[x[1] for x in r])
    return cache[ix]
rows=[]
for w in W:
    ser=w['series']
    if ser in variants.RECORD_ONLY: continue
    ci=cx(ser); ix=ci or bq(ser)
    if not ix: continue
    close=float(w['close_ts'])
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],close-480,close-120)).fetchall()
    pk=variants.favorite_entry([(float(a),float(b),float(c)) for a,b,c in q],close)
    if not pk: continue
    side,ask,at,bid=pk; ts,v=feed(ix)
    K=float(w['strike'])
    if not ci:
        i0=bisect.bisect_left(ts,close-900)
        if i0>=len(ts) or ts[i0]-(close-900)>20: continue
        K=v[i0]
    sg=1 if side=='yes' else -1
    def at_t(t):
        j=bisect.bisect_right(ts,t)-1
        return None if j<0 or t-ts[j]>10 else v[j]
    S=at_t(at); S30=at_t(at-30); S60=at_t(at-60); S120=at_t(at-120); S300=at_t(at-300)
    if None in (S,S30,S60,S120,S300): continue
    j=bisect.bisect_right(ts,at)-1; j0=bisect.bisect_right(ts,at-300)-1
    seg=v[j0:j+1]
    if len(seg)<30: continue
    sq=sum(math.log(b/a)**2 for a,b in zip(seg,seg[1:])); dt=ts[j]-ts[j0]
    if sq<=0 or dt<=0: continue
    rate=math.sqrt(sq/dt)                      # per sqrt(second)
    cush=lambda x: math.log(x/K)*sg
    c0=cush(S)
    d30=(c0-cush(S30))/(rate*math.sqrt(30)); d60=(c0-cush(S60))/(rate*math.sqrt(60)); d120=(c0-cush(S120))/(rate*math.sqrt(120))
    peak=max(cush(x) for x in seg)
    erosion=(peak-c0)/(rate*math.sqrt(300))
    c=10/ask; won=w['result']==side; pnl=((1-ask)*c if won else -10)-fee(ask)*c
    rows.append(dict(cls='crypto' if ci else 'commodity',series=ser,ask=ask,won=float(won),pnl=pnl,z=c0/(rate*math.sqrt(close-at)),d30=d30,d60=d60,d120=d120,erosion=erosion,left=close-at))
print(len(rows),'favorites with drift features')
json.dump(rows,open('/tmp/drift.json','w'))
b=base64.b64encode(gzip.compress(open('/tmp/drift.json','rb').read())).decode()
open('/tmp/drift.b64','w').write('\n'.join(b[i:i+1000] for i in range(0,len(b),1000))+'\n')
print('lines',len(b)//1000+1)
print('DONE')
