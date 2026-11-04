import os, time, bisect, statistics, collections
os.nice(15)
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
s=config.reload(); now=time.time(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
SER=("KX2YRRATE15M","KX5YRRATE15M","KX10YRRATE15M","KX30YRRATE15M")
W=st.db.execute("SELECT ticker,series,close_ts FROM windows WHERE series IN (?,?,?,?) AND result IN ('yes','no') AND close_ts>? ORDER BY close_ts",(*SER,now-3*86400)).fetchall()
byev=collections.defaultdict(dict)
for w in W: byev[round(float(w['close_ts']))][w['series']]=w['ticker']
STEP=5.0
def grid(ticker,close):
    q=tape.execute("SELECT ts,bid,ask,bid_size,ask_size FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(ticker,close-930,close)).fetchall()
    if len(q)<40: return None
    ts=[r[0] for r in q]; fl=tape.execute("SELECT ts,yes_ct,no_ct FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?",(ticker,close-930,close)).fetchall() if False else None
    out=[]; t=close-900
    while t<=close-45:
        k=bisect.bisect_right(ts,t)-1
        if k>=0 and t-ts[k]<20: r=q[k]; out.append((float(r[1]),float(r[2]),float(r[3] or 0),float(r[4] or 0)))
        else: out.append(None)
        t+=STEP
    return out
def flowgrid(ticker,close):
    cols=[r[1] for r in tape.execute("PRAGMA table_info(flow_1s)")] if not db.is_pg(tape) else None
    return None
rows=[]  # (own_past, others_past, imbalance, future, mid, minute)
H=6  # 30 s
for close,d in byev.items():
    if len(d)<4: continue
    gs={sr:grid(tk,float(close)) for sr,tk in d.items()}
    if any(g is None for g in gs.values()): continue
    n=min(len(g) for g in gs.values())
    for sr,g in gs.items():
        others=[o for k,o in gs.items() if k!=sr]
        for i in range(H,n-H):
            x=g[i]; p=g[i-H]; f=g[i+H]
            if not (x and p and f) or not (0.2<(x[0]+x[1])/2<0.8): continue
            mid=lambda z:(z[0]+z[1])/2
            oth=[mid(o[i])-mid(o[i-H]) for o in others if o[i] and o[i-H]]
            if len(oth)<3: continue
            tot=x[2]+x[3]
            imb=(x[2]-x[3])/tot if tot>0 else 0.0
            rows.append((mid(x)-mid(p), statistics.mean(oth), imb, mid(f)-mid(x), x[1]-x[0], i*STEP/60))
print('samples',len(rows))
def corr(a,b):
    try: return statistics.correlation(a,b)
    except Exception: return float('nan')
fut=[r[3] for r in rows]
print('corr(next 30s move, own past 30s move)   ',round(corr([r[0] for r in rows],fut),3))
print('corr(next 30s move, other 3 yields past) ',round(corr([r[1] for r in rows],fut),3))
print('corr(next 30s move, size imbalance)      ',round(corr([r[2] for r in rows],fut),3))
def decile(name,key,sign=1):
    srt=sorted(rows,key=key); n=len(srt)//10
    top=srt[-n:]; bot=srt[:n]
    mt=statistics.mean(r[3] for r in top)*100; mb=statistics.mean(r[3] for r in bot)*100
    print(f'{name:28} top decile next-30s move {mt:+.2f}c   bottom decile {mb:+.2f}c   (spread to cross: 4c)')
decile('other yields moved (30s)',lambda r:r[1])
decile('own past move',lambda r:r[0])
decile('book size imbalance',lambda r:r[2])
# combined: others up and own flat/down -> buy; measure
sel=[r for r in rows if r[1]>=0.05 and r[0]<=0.0]; sel2=[r for r in rows if r[1]<=-0.05 and r[0]>=0.0]
if sel: print('others up 5c+, own not up: n',len(sel),'next-30s own move',round(statistics.mean(r[3] for r in sel)*100,2),'c')
if sel2: print('others down 5c+, own not down: n',len(sel2),'next-30s own move',round(statistics.mean(r[3] for r in sel2)*100,2),'c')
print('DONE')
