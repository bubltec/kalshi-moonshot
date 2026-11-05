import os, time, math, statistics as S, bisect, itertools
os.nice(15)
from kalshi_moonshot import config, db, variants
s=config.reload(); tape=db.connect(s.tape_path,"tape",shared=True); now=time.time()
COINS=[('BTC','BRTI'),('ETH','ETHUSD_RTI'),('SOL','SOLUSD_RTI'),('XRP','XRPUSD_RTI'),('DOGE','DOGEUSD_RTI'),('ADA','ADAUSD_RTI'),('BCH','BCHUSD_RTI'),('BNB','BNBUSD_RTI'),('HYPE','HYPEUSD_RTI'),('NEAR','NEARUSD_RTI'),('ZEC','ZECUSD_RTI')]
start=now-30*3600   # the extra coins have about 28 h of ticks, so every coin is compared over the same last 30 h
print('Spot behaviour per coin over the same last 30 hours (1-second grid):')
print('coin   hourly vol  |  1-min return autocorr | variance ratio 5m/1m | 15-min windows: crossings of the open (avg, share with >=4) | avg 15-min range / vol')
for coin,ix in COINS:
    r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,start-1000)).fetchall()
    if len(r)<5000: print(f"{coin:5} too few ticks ({len(r)})"); continue
    ts=[float(x[0]) for x in r]; vs=[float(x[1]) for x in r]
    T0=int(start); T1=int(now)
    g=[]; j=0; last=None
    for T in range(T0-60,T1):
        while j<len(ts) and ts[j]<=T: last=vs[j]; j+=1
        g.append(last)
    lg=[math.log(x) if x else None for x in g]
    # 1-minute returns
    mins=[lg[k] for k in range(0,len(lg),60) if lg[k] is not None]
    r1=[b-a for a,b in itertools.pairwise(mins)]
    if len(r1)<200: continue
    sd1=S.pstdev(r1); m1=S.mean(r1)
    ac=sum((a-m1)*(b-m1) for a,b in itertools.pairwise(r1))/(len(r1)-1)/sd1**2
    r5=[mins[k+5]-mins[k] for k in range(0,len(mins)-5,5)]
    vr=(S.pvariance(r5)/(5*S.pvariance(r1))) if len(r5)>20 else float('nan')
    vol_h=sd1*math.sqrt(60)*1e4
    # 15-minute windows aligned to :00/:15/:30/:45 UTC
    first=((T0+899)//900)*900; cross=[];rng=[]
    for o in range(first,T1-900,900):
        k0=o-(T0-60)
        if k0<60 or k0+900>=len(lg): continue
        ref=[x for x in lg[k0-60:k0] if x is not None]
        if not ref: continue
        K=sum(ref)/len(ref)
        seg=[x for x in lg[k0:k0+900] if x is not None]
        if len(seg)<800: continue
        sg=[x>K for x in seg]; cross.append(sum(1 for a,b in itertools.pairwise(sg) if a!=b))
        rng.append((max(seg)-min(seg))/(sd1*math.sqrt(15)))
    print(f"{coin:5} {vol_h:7.1f} bp  |  {ac:+.3f}               | {vr:5.2f}                | {S.mean(cross):5.1f} crossings, {100*sum(1 for c in cross if c>=4)/len(cross):3.0f}% have >=4                  | {S.mean(rng):.2f}   (n={len(cross)} windows)")
print('DONE')
