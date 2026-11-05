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
def run(label,ents,tps):
    res={tp:[] for tp in tps}; peak=[]
    for (k,e,r0) in ents:
        wid,side=k; tk,opened,result=wmap[wid]; ask=r0[C['ask']]; c=10/ask; t_en=opened+e
        bs=bids_after(tk,side,t_en+1)
        mx=max([b for _,b in bs],default=0); peak.append(mx-ask)
        for tp in tps:
            hit=next((b for _,b in bs if b>=ask+tp),None)
            if hit is not None: p=c*(hit-ask)-c*fee(ask)-c*fee(hit)
            else: p=r0[C['pnl']]
            res[tp].append((p,hit is not None,r0[C['close']]))
    n=len(ents); cl=sorted(x[2] for x in res[tps[0]]); mid=cl[len(cl)//2]
    print(f"\n{label}: {n} entries. Peak bid reached after entry: median +{S.median(peak):.2f}; share reaching +.10: {100*sum(p>=.10 for p in peak)/n:.0f}%, +.20: {100*sum(p>=.20 for p in peak)/n:.0f}%, +.30: {100*sum(p>=.30 for p in peak)/n:.0f}%")
    for tp in tps:
        L=res[tp]; a=[x[0] for x in L if x[2]<mid]; b=[x[0] for x in L if x[2]>=mid]
        print(f"   take profit +{tp:.2f} (every quote update): hit {100*sum(x[1] for x in L)/n:3.0f}%  {S.mean(x[0] for x in L):+.2f}/bet  H1 {S.mean(a):+.2f} H2 {S.mean(b):+.2f}")
for cls,name in (('m','commodities'),('c','crypto')):
    run(f'P1 {name}: trend up on both, ask .45-.62, 1:00-5:00',entries(lambda r:0.45<=r[C['ask']]<=0.62 and r[C['r15']]>0.5 and r[C['r60']]>0.5,60,300,cls),(0.15,0.20,0.25,0.30))
    run(f'P1 {name}: no trend condition',entries(lambda r:0.45<=r[C['ask']]<=0.62,60,300,cls),(0.15,0.20,0.25,0.30))
    run(f'P2 {name}: swinging low price (.25-.45, range>=.15, >=2 swings), 2:00-10:00',entries(lambda r:0.25<=r[C['ask']]<=0.45 and r[C['orange']] is not None and r[C['orange']]>=0.15 and r[C['osw']]>=2,120,600,cls),(0.08,0.10,0.15,0.20))
print('DONE')
