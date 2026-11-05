import json, math, collections, time
import sys

from kalshi_moonshot import variants
variants.LEDGER_KEEP=1e12
R=json.load(open(__import__('sys').argv[1]))
for r in R: r['won']=bool(r['won'])
start=min(r['close_ts'] for r in R)+30*3600   # let the history build
def run(cls,mode):
    led=variants.Ledger(); led.add([r for r in R if r['cls']==cls])
    ev=[r for r in R if r['cls']==cls and r['ts']>=start]; mid=ev[len(ev)//2]['close_ts']
    cache={}; res={'all':[0,0.0,0,0.0],'H1':[0,0.0,0,0.0],'H2':[0,0.0,0,0.0]}
    for r in ev:
        key=(int(r['ts']//300),r['coin'] if mode=='ladder-coin' else None)
        if key not in cache:
            t=key[0]*300.0
            cache[key]=set(led.skips_ladder(cls,t,coin=key[1]) if mode.startswith('ladder') else led.skips(cls,t,6.0,15.0,0.10,5) if mode=='single6h' else ())
        sk=f"{r['variety']}-{r['side']}" in cache[key]
        for k in ('all','H1' if r['close_ts']<mid else 'H2'):
            x=res[k]
            if sk: x[2]+=1; x[3]+=r['pnl']
            else: x[0]+=1; x[1]+=r['pnl']
    return res
for cls in ('crypto','commodity'):
    print(f'=== {cls}')
    for mode in ('none','single6h','ladder','ladder-coin'):
        r=run(cls,mode)
        f=lambda x:f"bought {x[0]:4d} {x[1]:+7.1f} ({x[1]/max(x[0],1):+.2f}/bet) skipped {x[2]:4d} {x[3]:+7.1f}"
        print(f"  {mode:12} all: {f(r['all'])}\n{'':15}H1: {f(r['H1'])}\n{'':15}H2: {f(r['H2'])}")
