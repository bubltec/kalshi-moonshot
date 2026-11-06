"""A smarter exit from the market's own spot history (user: we built the spot/curve history, use it to decide). Every settled live commodity bet since 10-28 (one per market, side and lane; excluding multiscale) with the spot ticks and the bid path: at each quote after the buy the model's chance that our side wins is Phi(z), z = cushion / (that market's typical one-minute move over its last 15 minutes * sqrt(minutes left)), cushion = the bet side's log distance of the spot from the strike. Sell when the bid is at least `m` over that chance (the market pays more than the curve says it is worth), at the bid minus 1c; otherwise hold. Also a floor: sell when the chance has fallen under `f` while the bid is still over 0.05. Compared with holding and with the real exits; the bets that lost 70%+ shown separately."""
import calendar, math, bisect
from kalshi_moonshot import config, db, biquotefeed
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,'tape',shared=True)
Phi=lambda z: 0.5*(1+math.erf(z/math.sqrt(2)))
t0=calendar.timegm((2026,10,28,0,0,0))
rows=st.db.execute("SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND lane LIKE 'commodity_%' AND lane<>'commodity_multiscale' AND outcome IS NOT NULL ORDER BY ts",(t0,)).fetchall()
seen={}
for r in rows: seen[(r['ticker'],r['lane'],r['side'],round(r['ask'],3))]=r
def pnl_hold(r):
    c=r['stake_usd']/r['ask']
    return ((1-r['ask'])*c if r['outcome']==r['side'] else -r['ask']*c)-fee(r['ask'])*c
def pnl_actual(r):
    c=r['stake_usd']/r['ask']
    if r['exit_price'] is not None: return (r['exit_price']-r['ask'])*c-(fee(r['ask'])+fee(max(r['exit_price'],.01)))*c
    return pnl_hold(r)
data=[]
for r in seen.values():
    ser=r['ticker'].split('-')[0]; idx=biquotefeed.index_for_series(ser)
    w=st.db.execute("SELECT close_ts,strike FROM windows WHERE ticker=?",(r['ticker'],)).fetchone()
    if not idx or not w or not w['strike']: continue
    close=float(w['close_ts'])
    tk=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts",(idx,r['ts']-1000,close)).fetchall()
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts",(r['ticker'],r['ts'],close)).fetchall()
    if len(tk)<500 or len(q)<5: continue
    data.append((r,float(w['strike']),close,[x['ts'] for x in tk],[x['value'] for x in tk],q))
print('bets replayed',len(data),'| hold $%.2f | real exits $%.2f'%(sum(pnl_hold(r) for r,*_ in data),sum(pnl_actual(r) for r,*_ in data)))
def run(m,f=None,last=None):
    net=0.0; fired=0; helped=0.0; hurt=0.0; big=[0,0.0,0.0]
    for r,strike,close,T,V,q in data:
        sg=1 if r['side']=='yes' else -1; e=r['ask']; c=r['stake_usd']/e; h=pnl_hold(r); p=h
        for x in q:
            now=x['ts']; left=close-now
            if left<=10 or (last and left>last): continue
            i=bisect.bisect_right(T,now)-1
            if i<0 or now-T[i]>5: continue
            k0=bisect.bisect_left(T,now-900); seg=V[k0:i+1]
            if len(seg)<300: continue
            dd=[math.log(b2/a2) for a2,b2 in zip(seg,seg[1:])]
            unit=math.sqrt(sum(z*z for z in dd)/len(dd))*math.sqrt(60)   # per ~1 s sample, scaled to a minute
            if unit<=0: continue
            cush=sg*math.log(V[i]/strike)
            P=Phi(cush/(unit*math.sqrt(left/60.0)))
            b=x['bid'] if r['side']=='yes' else 1-x['ask']
            if (b>=P+m and b>=0.05) or (f is not None and P<f and b>=0.05):
                spx=max(b-0.01,0.01); p=(spx-e)*c-(fee(e)+fee(spx))*c; fired+=1; break
        net+=p
        if p>h: helped+=p-h
        elif p<h: hurt+=h-p
        if h<-0.7*r['stake_usd']: big[0]+=1; big[1]+=h; big[2]+=p
    print(f"  m={m:.2f} f={f} last={last}: net ${net:+8.2f} fired {fired:3d} helped ${helped:6.2f} hurt ${hurt:6.2f} | 70%+ losers {big[0]}: hold ${big[1]:+.2f} -> ${big[2]:+.2f}")
for m in (0.04,0.08,0.12,0.18,0.25): run(m)
for m in (0.12,0.18): run(m,last=300)
for f in (0.10,0.20): run(0.12,f)
print('DONE')
