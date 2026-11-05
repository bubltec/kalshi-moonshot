import os, time, calendar, bisect, json, gzip, base64
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
variants.load_custom(s)
t0=calendar.timegm((2026,11,2,6,0,0))
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(t0,)).fetchall()
cache={}
def feed(ix):
    if ix not in cache:
        r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,t0-1500)).fetchall(); cache[ix]=([float(x[0]) for x in r],[float(x[1]) for x in r])
    return cache[ix]
rows=[]; wid=0
for w in W:
    ser=w['series']
    if ser in variants.RECORD_ONLY: continue
    cx=variants.crypto_index(ser); ix=cx or variants.biquote_index(ser)
    if not ix: continue
    close=float(w['close_ts']); ts,vs=feed(ix); K=float(w['strike'])
    if not cx:
        i0=bisect.bisect_left(ts,close-900)
        if i0>=len(ts) or ts[i0]-(close-900)>20: continue
        K=vs[i0]
    q=[(float(a),float(b),float(c)) for a,b,c in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],close-500,close-90)).fetchall()]
    if not q: continue
    qt=[x[0] for x in q]; wid+=1
    for L in range(480,119,-10):
        at=close-L; i=bisect.bisect_right(qt,at)-1
        if i<0 or at-qt[i]>15: continue
        _,yb,ya=q[i]
        for side,bid,ask in (('yes',yb,ya),('no',1-ya,1-yb)):
            if 0.76<=ask<=0.92 and ask-bid<=0.03+1e-9:
                snap=variants.snapshot(ts,vs,K,side,at,close)
                if not snap: break
                won=w['result']==side; c=10/ask; pnl=((1-ask)*c if won else -10)-fee(ask)*c
                rows.append(dict(w=wid,cls='crypto' if cx else 'commodity',coin=variants.coin_of(ser),left=L,side=side,ask=round(ask,3),won=int(won),pnl=round(pnl,3),close=close,
                                 z=round(snap['z'],3),slope=round(snap['slope'],3),crosses=snap['crosses'],vr=round(snap['vol_ratio'],3),sc=round(snap['since_cross'],0),var=snap['variety']))
                break
json.dump(rows,open('/tmp/states.json','w'))
b=base64.b64encode(gzip.compress(open('/tmp/states.json','rb').read())).decode()
open('/tmp/states.b64','w').write('\n'.join(b[i:i+1000] for i in range(0,len(b),1000))+'\n')
print(len(rows),'obs; windows',wid,'; lines',len(b)//1000+1,'DONE')
