"""Commodity favorites since 11-02 (variants.favorites, one per window at the favorite's entry moment, ask as recorded, held to the result, $10 ticket after the fee): net by market x variety, by market x side, and by
market x price band, each in time thirds, to see which cells lose consistently and which skip rules are removing winners. A cell needs n >= 25."""
import time, calendar, collections, statistics as S
from kalshi_moonshot import config, variants
s=config.reload(); now=time.time(); t0=calendar.timegm((2026,11,2,0,0,0))
recs=[r for r in variants.favorites(s,t0,now-600) if r['cls']=='commodity']
recs.sort(key=lambda r:r['close_ts'])
print('commodity favorites',len(recs),'| net per $10',round(S.mean(r['pnl'] for r in recs),2),'| win',round(100*S.mean(r['won'] for r in recs)),'%')
n=len(recs); third=lambda r: min(2,recs.index(r)*3//n) if False else None
for i,r in enumerate(recs): r['t']=min(2,i*3//n)
def cell(name,L):
    if len(L)<25: return
    th=[[x['pnl'] for x in L if x['t']==k] for k in range(3)]
    print(f"  {name:30} n{len(L):4d} {S.mean(x['pnl'] for x in L):+6.2f} win {100*S.mean(x['won'] for x in L):3.0f}% | thirds {' '.join(f'{S.mean(t):+5.1f}({len(t)})' if t else '  -' for t in th)}")
band=lambda a: '.76-.84' if a<0.84 else '.84-.90' if a<0.90 else '.90+'
print('\nby market'); 
for c in sorted({r['coin'] for r in recs}): cell(c,[r for r in recs if r['coin']==c])
print('\nby market x side')
for c in sorted({r['coin'] for r in recs}):
    for sd in ('yes','no'): cell(f'{c} {sd}',[r for r in recs if r['coin']==c and r['side']==sd])
print('\nby market x variety (n>=25)')
for c in sorted({r['coin'] for r in recs}):
    for v in 'ABCDEFG': cell(f'{c} {v}',[r for r in recs if r['coin']==c and r['variety']==v])
print('\nby market x price band')
for c in sorted({r['coin'] for r in recs}):
    for b in ('.76-.84','.84-.90','.90+'): cell(f'{c} {b}',[r for r in recs if r['coin']==c and band(r['ask'])==b])
print('\nthe current skips, per market: what the skipped cells earned')
for c in sorted({r['coin'] for r in recs}):
    sk=[r for r in recs if (r['variety']=='G' and r['side']=='no') or (r['variety']=='A' and r['side']=='yes')]
    cell(f'{c} G-no or A-yes (skipped)',[r for r in sk if r['coin']==c]); cell(f'{c} everything else (taken)',[r for r in recs if r['coin']==c and r not in sk])
print('DONE')
