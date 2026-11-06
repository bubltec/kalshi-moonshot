"""Hard stop on the commodity lanes (user: a silver NO bought at 0.84 was sold at 0.03; we sold way too late). Every settled live commodity bet since 10-28 (one per market, side and lane; excluding multiscale) with its bid path from the tape: sell at the first bid at or under (1 - X) of the price paid, minus 1c of slip, for X in 20-60%. Compared with what actually happened (the real exits plus the result), by total and for the bets that lost 70%+ of the stake. Also prints the silver bet's bid path."""
import calendar, time, collections
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,'tape',shared=True)
def hm(t): return time.strftime('%H:%M:%S',time.gmtime(t-5*3600))
t0=calendar.timegm((2026,10,28,0,0,0))
rows=st.db.execute("SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price,exit_ts,exit_reason FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND lane LIKE 'commodity_%' AND lane<>'commodity_multiscale' AND (outcome IS NOT NULL OR exit_price IS NOT NULL) ORDER BY ts",(t0,)).fetchall()
seen={}
for r in rows: seen[(r['ticker'],r['lane'],r['side'],round(r['ask'],3))]=r
def actual(r):
    c=r['stake_usd']/r['ask']
    if r['exit_price'] is not None: return (r['exit_price']-r['ask'])*c-(fee(r['ask'])+fee(max(r['exit_price'],.01)))*c
    return ((1-r['ask'])*c if r['outcome']==r['side'] else -r['ask']*c)-fee(r['ask'])*c
def sidebid(side,q): return q['bid'] if side=='yes' else 1-q['ask']
data=[]
for r in seen.values():
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>=? AND bid>0 AND ask>0 ORDER BY ts",(r['ticker'],r['ts'])).fetchall()
    if len(q)<5: continue
    data.append((r,q))
print('bets with a bid path',len(data),'actual net $%.2f'%sum(actual(r) for r,_ in data))
for X in (0.20,0.30,0.40,0.50,0.60):
    net=0.0; fired=0; saved=0.0; cost=0.0; big=[0,0.0,0.0]
    for r,q in data:
        a=actual(r); e=r['ask']; c=r['stake_usd']/e; line=e*(1-X); p=a
        end=r['exit_ts'] or 1e18
        for x in q:
            if x['ts']>=end: break
            b=sidebid(r['side'],x)
            if b<=line:
                sp=max(b-0.01,0.01); p=(sp-e)*c-(fee(e)+fee(sp))*c; fired+=1; break
        net+=p
        if p!=a:
            if p>a: saved+=p-a
            else: cost+=a-p
        if a<-0.7*r['stake_usd']:
            big[0]+=1; big[1]+=a; big[2]+=p
    print(f"  stop at -{int(X*100)}%: net ${net:+8.2f} ({net-sum(actual(r) for r,_ in data):+.2f} vs actual)  fired {fired}  helped ${saved:.2f} hurt ${cost:.2f} | bets that lost 70%+: {big[0]}  actual ${big[1]:+.2f} -> ${big[2]:+.2f}")
for r in seen.values():
    if 'KXSILVER15M-26NOV061015' in r['ticker'] and r['side']=='no':
        print('silver NO',hm(r['ts']),'paid',r['ask'],'stake',round(r['stake_usd'],2),'exit',r['exit_price'],hm(r['exit_ts']) if r['exit_ts'] else None,r['exit_reason'][:60] if r['exit_reason'] else '')
        last=0
        for x in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? ORDER BY ts",(r['ticker'],r['ts']-5,r['ts']+240)):
            if x['ts']-last>=6: last=x['ts']; print('   ',hm(x['ts']),'NO bid',round(1-x['ask'],3))
print('DONE')
