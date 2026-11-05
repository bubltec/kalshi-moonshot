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

POL=[('hold',None)]
for T in (0.15,0.20,0.30): POL.append((f'TP+{T:.2f}',dict(tp=T)))
for S_ in (0.10,0.15,0.20):
    for T in (0.20,0.30): POL.append((f'stop-{S_:.2f} TP+{T:.2f}',dict(stop=S_,tp=T)))
for A in (0.10,0.15,0.20):
    for Rr in (0.05,0.08,0.12):
        for S_ in (None,0.15,0.25): POL.append((f'trail after+{A:.2f} give back {Rr:.2f}'+(f' stop-{S_:.2f}' if S_ else ''),dict(act=A,trail=Rr,stop=S_)))
def play(bs,ask,pnl_hold,pol):
    if pol is None: return pnl_hold
    c=10/ask; peak=ask; armed=False
    for _,b in bs:
        if 'tp' in pol and b>=ask+pol['tp']: return c*(b-ask)-c*fee(ask)-c*fee(b)
        if pol.get('stop') and b<=ask-pol['stop']: return c*(b-ask)-c*fee(ask)-c*fee(max(b,0.01))
        if 'trail' in pol:
            if b>peak: peak=b
            if peak>=ask+pol['act']: armed=True
            if armed and b<=peak-pol['trail']: return c*(b-ask)-c*fee(ask)-c*fee(max(b,0.01))
    return pnl_hold
def evaluate(label,ents,top=6):
    data=[]
    for (k,e,r0) in ents:
        wid,side=k; tk,opened,result=wmap[wid]; bs=bids_after(tk,side,opened+e+1)
        data.append((bs,r0[C['ask']],r0[C['pnl']],r0[C['close']],e))
    n=len(data); cl=sorted(d[3] for d in data); mid=cl[len(cl)//2]
    rows=[]
    for name,pol in POL:
        p=[play(bs,a,ph,pol) for bs,a,ph,cl_,e in data]
        a=[x for x,d in zip(p,data) if d[3]<mid]; b=[x for x,d in zip(p,data) if d[3]>=mid]
        rows.append((min(S.mean(a),S.mean(b)),S.mean(p),S.mean(a),S.mean(b),name))
    rows.sort(reverse=True)
    print(f"\n{label}: {n} entries; hold-to-result {[r for r in rows if r[4]=='hold'][0][1]:+.2f}/bet. Best 5 of {len(POL)} exits by the WORSE half:")
    for w_,m,a,b,name in rows[:5]: print(f"    {name:42} {m:+.2f}/bet  H1 {a:+.2f} H2 {b:+.2f}")
    pos=sum(1 for r in rows if r[0]>0); print(f"    exits positive in both halves: {pos} of {len(POL)}")
    return data
SETS=[('commodity','m'),('crypto','c')]
for name,cls in SETS:
    evaluate(f'{name}: ask .45-.62, 1:00-5:00, no condition',entries(lambda r:0.45<=r[C['ask']]<=0.62,60,300,cls))
    evaluate(f'{name}: ask .45-.62, trend up on 15 m and 1 h',entries(lambda r:0.45<=r[C['ask']]<=0.62 and r[C['r15']]>0.5 and r[C['r60']]>0.5,60,300,cls))
    evaluate(f'{name}: ask .25-.45, 2:00-10:00, swinging',entries(lambda r:0.25<=r[C['ask']]<=0.45 and r[C['orange']] is not None and r[C['orange']]>=0.15 and r[C['osw']]>=2,120,600,cls))
    evaluate(f'{name}: ask .25-.45, 1:00-4:00, no condition',entries(lambda r:0.25<=r[C['ask']]<=0.45,60,240,cls))
print('DONE')
