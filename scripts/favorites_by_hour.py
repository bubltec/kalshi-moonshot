"""Commodity and crypto favorites by New York hour (variants.favorites, since 10-28 for crypto and 11-02 for commodity): how many favorites exist per hour (a market with no spot movement has none) and what they earn per $10 ticket
after the fee. WTI and Brent shown on their own."""
import time, calendar, collections, statistics as S
from kalshi_moonshot import config, variants
s=config.reload(); now=time.time()
hr=lambda ts: time.gmtime(ts-5*3600).tm_hour
def table(name,L,days):
    by=collections.defaultdict(list)
    for r in L: by[hr(r['ts'])].append(r)
    print(f'\n{name}: {len(L)} favorites over {days:.1f} days')
    print('  ET hour: favorites per day / win% / net per $10')
    row=[]
    for h in range(24):
        v=by.get(h,[])
        row.append(f"{h:02d}h {len(v)/days:4.1f}/" + (f"{100*S.mean(r['won'] for r in v):2.0f}%/{S.mean(r['pnl'] for r in v):+.1f}" if len(v)>=6 else ' -'))
    for i in range(0,24,4): print('  '+'   '.join(row[i:i+4]))
t1=calendar.timegm((2026,11,2,0,0,0)); com=[r for r in variants.favorites(s,t1,now-600) if r['cls']=='commodity']; d=(now-t1)/86400
table('all commodity',com,d); table('WTI',[r for r in com if r['coin']=='WTI'],d); table('BRENT',[r for r in com if r['coin']=='BRENT'],d)
t2=calendar.timegm((2026,10,28,0,0,0)); cr=[r for r in variants.favorites(s,t2,now-600) if r['cls']=='crypto']; table('crypto',cr,(now-t2)/86400)
print('DONE')
