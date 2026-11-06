"""Per-market behaviour profile from the spot ticks (2026-11-06, user: each market's own curve and spot history drives it). For every 15-minute series and each look-back (1 h, 6 h, 24 h): the typical one-minute move in basis points, how many times the spot crosses the window's opening price per 15-minute window (flip rate), the window's high-low range in typical minutes (swing size), how often it is still on the other side of the open at minute 10 as at the close (late flips), and the efficiency ratio |net move| / path length (1 = a straight run, 0 = back and forth). Read-only."""
import os, time, math, bisect, collections
os.nice(15)
from kalshi_moonshot import config, db, kalshiws, biquotefeed
s=config.reload(); tape=db.connect(s.tape_path,"tape",shared=True); now=time.time()
SERIES=["KXBTC15M","KXETH15M","KXSOL15M","KXXRP15M","KXDOGE15M","KXGOLD15M","KXSILVER15M","KXPLATINUM15M","KXPALLADIUM15M","KXCOPPER15M","KXWTI15M","KXBRENT15M","KXNATGAS15M"]
def index(ser):
    return kalshiws.index_for_series(ser) or biquotefeed.index_for_series(ser)
def profile(ts,vs,t_end,hours):
    t_start=t_end-hours*3600
    i0=bisect.bisect_left(ts,t_start); i1=bisect.bisect_right(ts,t_end)
    T,V=ts[i0:i1],vs[i0:i1]
    if len(T)<600: return None
    # 1 s grid
    g=[];k=0
    for t in range(int(T[0]),int(T[-1])):
        while k+1<len(T) and T[k+1]<=t: k+=1
        g.append(math.log(V[k]))
    d=[b-a for a,b in zip(g,g[1:])]
    unit=math.sqrt(sum(x*x for x in d)/len(d))*math.sqrt(60)
    if unit<=0: return None
    base=int(T[0]); first=(int(T[0])//900+1)*900
    wins=[];cross=[];rng=[];late=[];eff=[]
    t0=first
    while t0+900<=int(T[-1]):
        a=t0-base
        seg=g[a:a+900]
        if len(seg)<900: break
        o=seg[0]; sg=[x-o for x in seg]
        cr=sum(1 for x,y in zip(sg,sg[1:]) if (x>0)!=(y>0) and y!=0)
        cross.append(cr); rng.append((max(seg)-min(seg))/unit)
        late.append(1 if (sg[600]>0)!=(sg[-1]>0) else 0)
        path=sum(abs(y-x) for x,y in zip(seg,seg[1:]))
        eff.append(abs(seg[-1]-seg[0])/path if path>0 else 0)
        t0+=900
    n=len(cross)
    if n<2: return None
    return dict(n=n,unit_bp=unit*1e4,cross=sum(cross)/n,rng=sum(rng)/n,late=100*sum(late)/n,eff=sum(eff)/n)
print(f"{'series':14}{'window':>7}{'n':>4}{'1m bp':>7}{'cross/15m':>10}{'range(min)':>11}{'late flip%':>11}{'efficiency':>11}")
for ser in SERIES:
    idx=index(ser)
    if not idx: print(ser,'no index'); continue
    rows=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(idx,now-24.2*3600)).fetchall()
    ts=[float(a) for a,_ in rows]; vs=[float(b) for _,b in rows]
    if len(ts)<1000: print(f"{ser:14} {idx}: only {len(ts)} ticks"); continue
    for h in (1,6,24):
        p=profile(ts,vs,now,h)
        if p: print(f"{ser:14}{str(h)+'h':>7}{p['n']:4d}{p['unit_bp']:7.1f}{p['cross']:10.1f}{p['rng']:11.1f}{p['late']:11.0f}{p['eff']:11.2f}")
print('DONE')
