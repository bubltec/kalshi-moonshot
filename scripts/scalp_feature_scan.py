import os, time, calendar, bisect, json, math
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,11,2,6,0,0))
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(t0,)).fetchall()
cache={}
def feed(ix):
    if ix not in cache:
        r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,t0-4300)).fetchall(); cache[ix]=([float(x[0]) for x in r],[float(x[1]) for x in r])
    return cache[ix]
def zig(vals,th):
    n=0; direction=0; ext=vals[0]
    for x in vals[1:]:
        if direction>=0 and x>ext: ext=x; direction=1 if direction==0 else direction
        elif direction<=0 and x<ext: ext=x; direction=-1 if direction==0 else direction
        if direction==1 and ext-x>=th: n+=1; direction=-1; ext=x
        elif direction==-1 and x-ext>=th: n+=1; direction=1; ext=x
    return n
rows=[]; wid=0
for w in W:
    ser=w['series']
    if ser in variants.RECORD_ONLY: continue
    cx=variants.crypto_index(ser); ix=cx or variants.biquote_index(ser)
    if not ix: continue
    close=float(w['close_ts']); opened=close-900; ts,vs=feed(ix)
    def val(T):
        i=bisect.bisect_right(ts,T)-1
        return vs[i] if i>=0 and T-ts[i]<60 else None
    q=[(float(a),float(b),float(c)) for a,b,c in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],opened-20,close)).fetchall()]
    if len(q)<20: continue
    qt=[x[0] for x in q]; wid+=1; coin=variants.coin_of(ser)
    mids={}
    for e in range(0,871,30):
        i=bisect.bisect_right(qt,opened+e)-1
        if i>=0 and opened+e-qt[i]<=20: mids[e]=(q[i][1]+q[i][2])/2
    for e in range(60,871,30):
        at=opened+e; i=bisect.bisect_right(qt,at)-1
        if i<0 or at-qt[i]>20 or e not in mids: continue
        _,yb,ya=q[i]
        now_v=val(at)
        if now_v is None: continue
        # spot trend over 15 min and 60 min, in units of the hour's typical move
        minute=[val(at-60*k) for k in range(0,61)]
        if any(m is None for m in minute): continue
        rets=[math.log(minute[k]/minute[k+1]) for k in range(60)]
        sd=(sum(r*r for r in rets)/60)**0.5
        if sd<=0: continue
        r15=math.log(minute[0]/minute[15])/(sd*math.sqrt(15)); r60=math.log(minute[0]/minute[60])/(sd*math.sqrt(60)); r5=math.log(minute[0]/minute[5])/(sd*math.sqrt(5))
        hist=[mids[k] for k in range(max(0,e-300),e+1,30) if k in mids]
        orange=max(hist)-min(hist) if len(hist)>=6 else None
        osw=zig(hist,0.06) if len(hist)>=6 else None
        for side,bid,ask in (('yes',yb,ya),('no',1-ya,1-yb)):
            if not (0.2<=ask<=0.78): continue
            sg=1 if side=='yes' else -1
            won=w['result']==side; c=10/ask; pnl=((1-ask)*c if won else -10)-fee(ask)*c
            rows.append((wid,'c' if cx else 'm',coin,e,side[0],round(ask,3),round(ask-bid,3),int(won),round(pnl,3),close,round(sg*r15,2),round(sg*r60,2),round(sg*r5,2),None if orange is None else round(orange,3),osw))
# forward points are needed for the sims: keep every (wid, side, e) row; bids can be rebuilt as ask-spread
json.dump(rows,open('/tmp/curve31.json','w'))
print(len(rows),'rows; windows',wid,'DONE')
