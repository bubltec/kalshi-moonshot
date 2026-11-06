"""Sell on a bounce off the spot's best level and buy the other side, by curve shape (2026-11-06). Read-only.

Every settled live bet of the last 8 days with a quote path. In the last 300 s the first time the spot has bounced A typical minutes off its best level for the bet's side within 300 s, sell at the bid and buy an equal number of contracts of the other side at 1 - that bid, fees on both legs, then settle. Change = this minus what the bet actually returned. Results are split by the spot's path efficiency over the 5 minutes before the signal (terciles), by asset class and by half of the period (the split by asset class is a check, not a rule: see CLAUDE.md).

Run on the host: scripts/hostrun.sh-style, GROUP=all.
"""
import time, calendar, os
from collections import defaultdict
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,'tape',shared=True)
GROUP=os.environ['GROUP']; DAYS=8
CRYPTO=('KXBTC','KXETH','KXSOL','KXXRP','KXDOGE')
rows=st.db.execute("SELECT ts,lane,ticker,side,contracts,stake_usd,ask,outcome,exit_price,exit_ts,close_time FROM decisions WHERE mode='trade' AND action='bet' AND stake_usd>0.5 AND ts>? AND outcome IN ('yes','no') ORDER BY ts",(time.time()-DAYS*86400,)).fetchall()
def iscrypto(t): return t.startswith(CRYPTO)
bets=[]
for r in rows:
    pass
    if 'RATE' in r['ticker']: continue
    try: close=calendar.timegm(time.strptime(r['close_time'][:19],'%Y-%m-%dT%H:%M:%S'))
    except Exception: continue
    n=r['contracts'] or r['stake_usd']/r['ask']; cost=r['stake_usd']; entry=cost/n
    end=r['exit_ts'] if r['exit_ts'] else close
    actual=(n*r['exit_price']-cost) if r['exit_price'] is not None else ((n-cost) if r['outcome']==r['side'] else -cost)
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? ORDER BY ts",(r['ticker'],r['ts'],end)).fetchall()
    if len(q)<20: continue
    path=[]
    for x in q:
        if x['bid'] is None or x['ask'] is None: continue
        mine=float(x['bid']) if r['side']=='yes' else 1-float(x['ask'])
        path.append((float(x['ts']),mine))
    if len(path)<20: continue
    bets.append((r['ts'],n,cost,entry,actual,path,end,r['ticker'],r['side'],r['outcome']))
from kalshi_moonshot import marketprofile
import bisect, math
SP={}
for b in bets:
    ser=b[7].split('-')[0]; idx=marketprofile._index(ser)
    if not idx: continue
    d=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts",(idx,b[0]-1000,b[6]+1)).fetchall()
    if len(d)>300: SP[b[7]+b[8]+str(b[0])]=([float(r[0]) for r in d],[float(r[1]) for r in d])
bets=[b for b in bets if b[7]+b[8]+str(b[0]) in SP]
print(GROUP,'bets with quote paths',len(bets),'actual total',round(sum(b[4] for b in bets),2))
def drift_against(T,V,side,at,span):
    """How far the spot has bounced off its best level for our side over the last span seconds, in typical minutes (our side's extreme: the low for NO, the high for YES)."""
    j=bisect.bisect_right(T,at)-1; k=bisect.bisect_right(T,at-span)-1
    if j<0 or k<0 or at-T[j]>10: return None
    lo=bisect.bisect_right(T,at-900); seq=V[lo:j+1]
    if len(seq)<300: return None
    st=[math.log(b/a) for a,b in zip(seq,seq[1:]) if a>0 and b>0]
    unit=math.sqrt(sum(x*x for x in st)/len(st))*math.sqrt(60.0) if st else 0
    if unit<=0: return None
    win=V[k:j+1]
    return math.log(V[j]/min(win))/unit if side=='no' else math.log(max(win)/V[j])/unit
from collections import defaultdict
def eff_of(T,V,at,span=300):
    j=bisect.bisect_right(T,at)-1; k=bisect.bisect_right(T,at-span)-1
    seg=V[k:j+1]
    if len(seg)<30: return None
    path=sum(abs(math.log(b/a)) for a,b in zip(seg,seg[1:]) if a>0 and b>0)
    return abs(math.log(seg[-1]/seg[0]))/path if path>0 else None
rows_out=[]
A,SPAN,F,CAP,LAST=3.0,300,1.0,1.0,300
for ts,n,cost,entry,actual,path,end,tk,side,outc in bets:
    T_,V_=SP[tk+side+str(ts)]
    for t,b in path[::5]:
        if end-t>LAST: continue
        d=drift_against(T_,V_,side,t,SPAN)
        if d is not None and d>=A:
            sold=n*b-cost-fee_per_contract(b)*n; ob=1-b; flip=0.0
            if 0.02<=ob<=1.0:
                m=n*F; won=(outc!=side); flip=m*(1.0 if won else 0.0)-m*ob-fee_per_contract(ob)*m
            rows_out.append((tk.split('-')[0],sold-actual,sold+flip-actual,eff_of(T_,V_,t),ob,ts,iscrypto(tk))); break

rows_out=[r for r in rows_out if r[3] is not None]
effs=sorted(r[3] for r in rows_out); q1,q2=effs[len(effs)//3],effs[2*len(effs)//3]
tsmed=sorted(r[5] for r in rows_out)[len(rows_out)//2]
def band(e): return 'choppy' if e<q1 else ('middle' if e<q2 else 'straight')
print('cuts: choppy <',round(q1,3),' straight >=',round(q2,3))
print('sell+flip change by curve shape, split by asset class and by half of the 8 days')
for lab,f in (('all',lambda r:True),('crypto only',lambda r:r[6]),('commodity only',lambda r:not r[6]),('first half',lambda r:r[5]<=tsmed),('second half',lambda r:r[5]>tsmed)):
    cells=[]
    for b in ('choppy','middle','straight'):
        v=[r for r in rows_out if f(r) and band(r[3])==b]
        cells.append(f'{b}: n={len(v):3} {sum(x[2] for x in v):+7.2f}')
    print(f' {lab:15}',' | '.join(cells))
print("DONE")
