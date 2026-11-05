import os, calendar, bisect, json, collections, statistics as S
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,11,2,6,0,0))
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(t0,)).fetchall()
wmap={}; wid=0
for w in W:
    ser=w['series']
    if ser in variants.RECORD_ONLY: continue
    cx=variants.crypto_index(ser); ix=cx or variants.biquote_index(ser)
    if not ix: continue
    n=tape.execute("SELECT count(*) FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0",(w['ticker'],float(w['close_ts'])-920,float(w['close_ts']))).fetchone()[0]
    if n<20: continue
    wid+=1; wmap[wid]=(w['ticker'],float(w['close_ts'])-900,w['result'])
R=json.load(open('/tmp/curve31.json'))
K='wid cls coin e side ask spread won pnl close r15 r60 r5 orange osw'.split(); C={k:i for i,k in enumerate(K)}
by=collections.defaultdict(dict)
for r in R: by[(r[0],r[C['side']])][r[C['e']]]=r
def entries(ok,e_lo,e_hi,cls):
    out=[]
    for k,d in by.items():
        for e in range(e_lo,e_hi+1,30):
            r=d.get(e)
            if r and r[C['cls']]==cls and r[C['spread']]<=0.04 and ok(r): out.append((k,e,r)); break
    return out
qcache={}
def bids_after(tk,side,t_entry):
    q=qcache.get(tk)
    if q is None:
        q=[(float(a),float(b),float(c)) for a,b,c in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 ORDER BY ts",(tk,)).fetchall()]; qcache[tk]=q
    i=bisect.bisect_right([x[0] for x in q],t_entry)
    return [(x[0], x[1] if side=='y' else 1-x[2]) for x in q[i:]]


import datetime

rows_out=[]
for cls,name in (('c','crypto'),('m','commodity')):
    for k,d in by.items():
        for e in range(60,301,30):
            r=d.get(e)
            if not r or r[C['cls']]!=cls or not (0.25<=r[C['ask']]<=0.65) or r[C['spread']]>0.04: continue
            wid,side=k; tk,opened,result=wmap[wid]; ask=r[C['ask']]; c=10/ask
            def a_at(ee):
                x=d.get(ee); return x[C['ask']] if x else None
            p30,p60,p120=a_at(e-30),a_at(e-60),a_at(e-120)
            bs=bids_after(tk,side,opened+e+1)
            res={}
            for tp in (0.15,0.20):
                hit=next((b for _,b in bs if b>=ask+tp),None)
                res[tp]=(c*(hit-ask)-c*fee(ask)-c*fee(hit)) if hit is not None else r[C['pnl']]
            rows_out.append(dict(cls=cls,coin=r[C['coin']],w=wid,side=side,e=e,ask=ask,spread=r[C['spread']],r5=r[C['r5']],r15=r[C['r15']],r60=r[C['r60']],orange=r[C['orange']],osw=r[C['osw']],
                                 p30=p30,p60=p60,p120=p120,close=r[C['close']],won=r[C['won']],pnl15=res[0.15],pnl20=res[0.20]))
            break  # first qualifying point per market and side
json.dump(rows_out,open('/tmp/svm_entries.json','w'))
print(len(rows_out),'entries DONE')
