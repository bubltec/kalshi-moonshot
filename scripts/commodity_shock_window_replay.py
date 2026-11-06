"""How early should the commodity shock exit be allowed to fire (user: a silver NO bought at 0.84 was sold at 0.03 at 120 s left; "we sold way too late")? Every settled live commodity bet since 10-28 (one per market, side and lane; excluding multiscale) with its spot ticks and bid path: the lane's shock rule (spot has moved shock_bp bp against the bet over shock_seconds, the worst of the last shock_memory seconds, and sits within shock_margin_bp of the strike; bid between 0.05 and 0.89) replayed with the window set to the last 120, 180, 240 and 300 s. Sale at the bid minus 1c. Net compared with holding to the result."""
import calendar, math, bisect
from kalshi_moonshot import config, db, biquotefeed, lanes
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,'tape',shared=True)
specs=lanes.lane_specs(s)
t0=calendar.timegm((2026,10,28,0,0,0))
rows=st.db.execute("SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price,exit_ts FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND lane LIKE 'commodity_%' AND lane<>'commodity_multiscale' AND outcome IS NOT NULL ORDER BY ts",(t0,)).fetchall()
seen={}
for r in rows: seen[(r['ticker'],r['lane'],r['side'],round(r['ask'],3))]=r
def pnl_hold(r):
    c=r['stake_usd']/r['ask']
    return ((1-r['ask'])*c if r['outcome']==r['side'] else -r['ask']*c)-fee(r['ask'])*c
data=[]
for r in seen.values():
    ser=r['ticker'].split('-')[0]; idx=biquotefeed.index_for_series(ser)
    w=st.db.execute("SELECT close_ts,strike FROM windows WHERE ticker=?",(r['ticker'],)).fetchone()
    sp=specs.get(r['lane'])
    if not idx or not w or not w['strike'] or sp is None or sp.shock_bp<=0 or ser in sp.shock_off_series: continue
    close=float(w['close_ts'])
    tk=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts",(idx,r['ts']-1500,close)).fetchall()
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? AND bid>0 AND ask>0 ORDER BY ts",(r['ticker'],r['ts'],close)).fetchall()
    if len(tk)<200 or len(q)<5: continue
    data.append((r,sp,float(w['strike']),close,[x['ts'] for x in tk],[x['value'] for x in tk],q))
print('bets replayed',len(data),'hold net $%.2f'%sum(pnl_hold(r) for r,*_ in data))
def run(L,hold=None):
    net=0.0; fired=0; better=0.0; worse=0.0; big=[0,0.0,0.0]
    for r,sp,strike,close,T,V,q in data:
        sg=1 if r['side']=='yes' else -1; e=r['ask']; c=r['stake_usd']/e; h=pnl_hold(r); p=h
        moves=[]
        for x in q:
            now=x['ts']; left=close-now
            if left>L or left<=3: continue
            i=bisect.bisect_right(T,now)-1
            if i<0 or now-T[i]>5: continue
            j=bisect.bisect_right(T,now-sp.shock_seconds)-1
            if j<0: continue
            mv=math.log(V[i]/V[j])*sg*1e4; moves.append((now,mv))
            while moves and now-moves[0][0]>max(sp.shock_memory,1): moves.pop(0)
            worst=min(m for _,m in moves)
            margin=math.log(V[i]/strike)*sg*1e4
            b=x['bid'] if r['side']=='yes' else 1-x['ask']
            if hold is not None and worst<=-sp.shock_bp and margin<=sp.shock_margin_bp:
                k0=bisect.bisect_left(T,now-900)
                seg=V[k0:i+1]
                if len(seg)>=300:
                    d=[math.log(b2/a2) for a2,b2 in zip(seg,seg[1:])]
                    unit=math.sqrt(sum(z*z for z in d)/len(d))*math.sqrt(60)
                    ia=bisect.bisect_right(T,now-90)-1; ib=bisect.bisect_right(T,now-90-420)-1
                    if unit>0 and ia>=0 and ib>=0 and sg*math.log(V[ia]/V[ib])/unit>=hold: continue
            if worst<=-sp.shock_bp and margin<=sp.shock_margin_bp and 0.05<=b<=0.89:
                spx=max(b-0.01,0.01); p=(spx-e)*c-(fee(e)+fee(spx))*c; fired+=1; break
        net+=p
        if p>h: better+=p-h
        elif p<h: worse+=h-p
        if h<-0.7*r['stake_usd']: big[0]+=1; big[1]+=h; big[2]+=p
    print(f"  last {L:3d}s{' hold with trend>='+str(hold) if hold is not None else '':26}: net ${net:+8.2f} fired {fired:3d} helped ${better:6.2f} hurt ${worse:6.2f} | 70%+ losers {big[0]}: hold ${big[1]:+.2f} -> ${big[2]:+.2f}")
for L in (120,180,300): run(L)
for L in (120,180,300): run(L,0.5)
print('DONE')
