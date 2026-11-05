import os, time, collections
os.nice(15)
from kalshi_moonshot import config, variants
from kalshi_moonshot.store import Store
variants.LEDGER_KEEP = 1e12
s=config.reload(); st=Store(s.db_path,s.exclude_versions)
import calendar
t0=calendar.timegm((2026,10,28,5,0,0))
rows=list(variants.favorites(s,t0,time.time()-600))
led=variants.Ledger(); led.add(rows)
first=min(r['close_ts'] for r in rows); start=first+96*3600
HOURS=(2,4,6,12,24,48,92)
print('favorites',len(rows),'from',time.strftime('%m-%d %H:%M',time.gmtime(first)),'; judged from',time.strftime('%m-%d %H:%M',time.gmtime(start)),'(96 h in, so every window has its history)')
for cls in ('crypto','commodity'):
    ev=[r for r in rows if r['cls']==cls and r['ts']>=start]
    mid=ev[len(ev)//2]['close_ts']
    print(f"\n=== {cls}: {len(ev)} favorites judged; no skipping = {sum(r['pnl'] for r in ev):+.1f} ({sum(r['pnl'] for r in ev)/len(ev):+.2f}/bet)")
    for H in HOURS:
        cache={}
        res={'all':[0,0.0,0,0.0],'H1':[0,0.0,0,0.0],'H2':[0,0.0,0,0.0]}
        for r in ev:
            b=int(r['ts']//300)
            if b not in cache: cache[b]=set(led.skips(cls,b*300.0,H,15.0,0.10,5))
            skipped=f"{r['variety']}-{r['side']}" in cache[b]
            for k in ('all','H1' if r['close_ts']<mid else 'H2'):
                x=res[k]
                if skipped: x[2]+=1; x[3]+=r['pnl']
                else: x[0]+=1; x[1]+=r['pnl']
        def f(x): return f"bought {x[0]:4d} {x[1]:+7.1f} ({x[1]/max(x[0],1):+.2f}/bet) | skipped {x[2]:4d} {x[3]:+7.1f} ({x[3]/max(x[2],1):+.2f}/bet)"
        print(f"{H:3d}h  all: {f(res['all'])}")
        print(f"       H1: {f(res['H1'])}\n       H2: {f(res['H2'])}")
print('DONE')
