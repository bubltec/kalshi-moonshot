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
def zigzag(vals,th):
    """Legs of a path: reversals of at least th from the running extreme. Returns [(direction +1/-1, size)] oldest first."""
    if len(vals)<3: return []
    legs=[]; direction=0; ext=vals[0]; start=vals[0]
    for x in vals[1:]:
        if direction>=0 and x>ext: ext=x; direction=1 if x-start>=th or direction==1 else direction
        elif direction<=0 and x<ext: ext=x; direction=-1 if start-x>=th or direction==-1 else direction
        if direction==1 and ext-x>=th: legs.append((1,ext-start)); start=ext; ext=x; direction=-1
        elif direction==-1 and x-ext>=th: legs.append((-1,start-ext)); start=ext; ext=x; direction=1
        elif direction==0 and abs(x-start)>=th: direction=1 if x>start else -1; ext=x
    if direction!=0: legs.append((direction,abs(ext-start)))
    return legs
rows=[]
for w in W:
    ser=w['series']
    if ser in variants.RECORD_ONLY: continue
    ci=cx(ser); ix=ci or bq(ser)
    if not ix: continue
    close=float(w['close_ts']); open_=close-900
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],close-480,close-100)).fetchall()
    for label,lo,hi in (('std',120,480),('late',100,330)):
        pk=variants.favorite_entry([(float(a),float(b),float(c)) for a,b,c in q if lo<=close-a<=hi],close,left=(hi,lo))
        if not pk: continue
        side,ask,at,bid=pk; ts,v=feed(ix); K=float(w['strike'])
        if not ci:
            i0=bisect.bisect_left(ts,open_)
            if i0>=len(ts) or ts[i0]-open_>20: continue
            K=v[i0]
        sg=1 if side=='yes' else -1
        a=bisect.bisect_left(ts,open_); b=bisect.bisect_right(ts,at)
        seg=v[a:b]
        if len(seg)<120: continue
        cush=[math.log(x/K)*sg for x in seg[::5]]            # 5 s grid, cushion in the favorite's favor
        sq=sum(math.log(y/x)**2 for x,y in zip(seg,seg[1:])); dt=ts[b-1]-ts[a]
        if sq<=0 or dt<=0: continue
        rate=math.sqrt(sq/dt); th=rate*math.sqrt(60)          # a typical one-minute move
        legs=zigzag(cush,th)
        now_c=cush[-1]; mx=max(cush); mn=min(cush)
        rng=mx-mn if mx>mn else 1e-9
        on_side=sum(1 for c in cush if c>0)/len(cush)
        cross=sum(1 for x,y in zip(cush,cush[1:]) if (x>0)!=(y>0))
        dirs=[d for d,_ in legs]
        relapse = len(legs)>=3 and dirs[-1]==-1 and dirs[-2]==1 and dirs[-3]==-1     # erode, recover, erode again (the shape the user described)
        lower_high = len(legs)>=3 and dirs[-1]==-1 and dirs[-2]==1 and legs[-2][1]<legs[-3][1] if len(legs)>=3 else False
        cn=10/ask; won=w['result']==side; pnl=((1-ask)*cn if won else -10)-fee(ask)*cn
        rows.append(dict(when=label,cls='crypto' if ci else 'commodity',ask=ask,won=float(won),pnl=pnl,close=close,left=close-at,
            nlegs=len(legs),last_dir=(dirs[-1] if dirs else 0),last_size=(legs[-1][1]/th if legs else 0.0),prev_size=(legs[-2][1]/th if len(legs)>1 else 0.0),
            retr=(mx-now_c)/rng,onside=on_side,cross=cross,relapse=relapse,lower_high=lower_high,z=now_c/(rate*math.sqrt(max(close-at,1))),cmax=mx/(rate*math.sqrt(60)),cmin=mn/(rate*math.sqrt(60))))
print(len(rows),'rows')
json.dump(rows,open('/tmp/swing.json','w'))
b=base64.b64encode(gzip.compress(open('/tmp/swing.json','rb').read())).decode()
open('/tmp/swing.b64','w').write('\n'.join(b[i:i+1000] for i in range(0,len(b),1000))+'\n')
print('lines',len(b)//1000+1,'DONE')
