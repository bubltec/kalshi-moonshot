"""Were the overnight WTI/Brent favorites ever evaluated? For each commodity favorite since 11-02 (variants.favorites), is there a decision row (any lane, any action) for that ticker? Shown by New York hour for WTI and Brent,
with the reason of the first row for those that were evaluated and passed."""
import time, calendar, collections
from kalshi_moonshot import config, variants
from kalshi_moonshot.store import Store
s=config.reload(); st=Store(s.db_path,s.exclude_versions); now=time.time(); t1=calendar.timegm((2026,11,2,0,0,0))
hr=lambda ts: time.gmtime(ts-5*3600).tm_hour
fav=[r for r in variants.favorites(s,t1,now-600) if r['cls']=='commodity' and r['coin'] in ('WTI','BRENT')]
seen={x['ticker'] for x in st.db.execute("SELECT DISTINCT ticker FROM decisions WHERE ticker LIKE 'KXWTI15M%' OR ticker LIKE 'KXBRENT15M%'")}
by=collections.defaultdict(lambda:[0,0])
for r in fav:
    h=hr(r['ts']); by[h][0]+=1; by[h][1]+=(r['ticker'] in seen)
print('WTI+Brent favorites since 11-02: total',len(fav),'with a decision row',sum(v[1] for v in by.values()))
print('ET hour: favorites / evaluated')
print('  '+'  '.join(f"{h:02d}h {by[h][0]}/{by[h][1]}" for h in range(24)))
miss=[r for r in fav if r['ticker'] not in seen and 3<=hr(r['ts'])<=4]
print('unevaluated 03-04h favorites:',len(miss))
for r in miss[:8]: print('  ',time.strftime('%m-%d %H:%M',time.gmtime(r['ts'])),r['ticker'],r['side'],f"{r['ask']:.2f}",'variety',r['variety'],'z',round(r['z'],2),'won',r['won'])
print('DONE')
