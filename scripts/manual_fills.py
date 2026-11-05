import time, json, collections
from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.store import Store
s=config.reload(); st=Store(s.db_path,s.exclude_versions); now=time.time()
c=KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
rows=c.portfolio_rows("/portfolio/fills","fills",int(now-8*3600),pages=6)
bot={r[0] for r in st.db.execute("SELECT DISTINCT ticker FROM decisions WHERE action='bet' AND mode='trade' AND ts>?",(now-8*3600,)).fetchall()}
by=collections.defaultdict(list)
for r in rows: by[r.get('market_ticker') or r.get('ticker')].append(r)
print('tickers filled in 8h:',len(by),'; with a bot bet:',len([t for t in by if t in bot]))
for tk,L in sorted(by.items(), key=lambda kv:min(str(x.get('created_time')) for x in kv[1])):
    tag='BOT' if tk in bot else 'NO BOT BET'
    acts=collections.Counter((x.get('action'),x.get('side')) for x in L)
    if tag=='NO BOT BET':
        print(tk,tag)
        for x in sorted(L,key=lambda x:str(x.get('created_time'))):
            print('   ',x.get('created_time'),x.get('action'),x.get('side'),'count',x.get('count_fp') or x.get('count'),'yes',x.get('yes_price_dollars'),'no',x.get('no_price_dollars'),'taker',x.get('is_taker'),'fee',x.get('fee_cost'))
print('DONE')
