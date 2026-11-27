"""Which entry readings do the losing PriskP buys share? (2026-11-26; user, after BTC, ZEC and DOGE 22:30 NO and HYPE and XRP 22:45 YES went to zero together: "It's an entry issue when they all fail like that. The exit failing was just the safety net".)
QUESTION (one, on the lane's own bets): every live PriskP bet since the per-tick entry change (2026-11-25 15:57 ET) with a settled market, tabulated by the readings its own buy note carries (why, regime, minute, the model's chance, the fifths, the swing, the hour's drift, the price paid, how many same-side bets the lane had already bought in the window): how often the market settled the bet's way, what the bet made as run (its sale after the fee, or settlement) and what it would have made held to settlement. The buys rested (no buy fee).
HONEST LIMITS: a table, not a rule: every split was looked at after the results, nothing is a setting, the readings overlap (a low chance, a middle cushion and a cheap price are largely the same bets), and it is 1.6 days. No asset is a key. A lead for one stated rule scored forward.
INPUT: the rows of this read-only query, one per line (ticker|side|ts|paid|contracts|exit_price|result|buy note from 'priskp yes ('):
  SELECT d.ticker||'|'||d.side||'|'||d.ts||'|'||(d.stake_usd/d.contracts)||'|'||d.contracts||'|'||coalesce(d.exit_price::text,'')||'|'||coalesce(w.result,'')||'|'||replace(substr(d.reason, position('priskp yes (' in d.reason)+12, 520),'|','/') FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.lane='priskp' AND d.action='bet' AND d.mode='trade' AND d.stake_usd>0 AND d.ts>=extract(epoch from timestamptz '2026-11-25 15:57:00-05') ORDER BY d.ts
Run: .venv/bin/python scripts/priskp_entry_readings_table.py ROWS.txt"""
import sys, re, collections, statistics
fee=lambda p:0.07*p*(1-p)
rows=[]
def num(x):
    try: return float(x)
    except (TypeError, ValueError): return None
for l in open(sys.argv[1]):
    f=l.rstrip("\n").split("|")
    if len(f)<8 or not f[0].startswith("KX") or f[6] not in("yes","no"): continue
    tk,side,ts,paid,n,xp,res,why=f[0],f[1],float(f[2]),float(f[3]),float(f[4]),f[5],f[6],f[7]
    held=(1.0 if res==side else 0.0)-paid
    pnl=((float(xp)-fee(float(xp))) if xp else (1.0 if res==side else 0.0))-paid
    g=lambda k: (m.group(1) if (m:=re.search(k+r": ([-\w\.]+)",why)) else None)
    sw=re.search(r"swing ([\d\.]+) u after a turn of ([\d\.]+) u",why)
    rows.append(dict(tk=tk,side=side,ts=ts,paid=paid,n=n,pnl=pnl*n,held=held*n,won=res==side,minute=num(g("minute")),chance=num(g("chance")),why=g("why"),cush=g("cushion_fifth"),trend=g("trend_fifth"),hist=g("history_fifth"),price=g("price_fifth"),confirm=num(g("confirm")),swing=float(sw.group(1)) if sw else None,drift=num(g("hour_drift")),regime=g("regime"),win=tk.split("-")[1]))
for r in rows:
    r["stack"]=sum(1 for o in rows if o is not r and o["win"]==r["win"] and o["side"]==r["side"] and o["ts"]<r["ts"])
    r["with_drift"]=None if r["drift"] is None else ("with" if (r["drift"]>0)==(r["side"]=="yes") else "against")
print(len(rows),"bets, as run $%.2f, held to settlement $%.2f, settled on the bet's side %d"%(sum(r["pnl"] for r in rows),sum(r["held"] for r in rows),sum(r["won"] for r in rows)))
def tab(name,key,order=None):
    d=collections.defaultdict(list)
    for r in rows:
        k=key(r)
        if k is not None: d[k].append(r)
    print("\n"+name)
    for k in (order or sorted(d, key=str)):
        v=d.get(k,[])
        if v: print(f"  {str(k):14s} n {len(v):3d}  settled its way {sum(x['won'] for x in v)/len(v):4.0%}  avg paid {statistics.mean(x['paid'] for x in v):.2f}  as run ${sum(x['pnl'] for x in v):8.2f} ({sum(x['pnl'] for x in v)/len(v):+.2f} a bet)  held ${sum(x['held'] for x in v):8.2f}")
tab("by why", lambda r:r["why"]); tab("by regime", lambda r:r["regime"])
tab("by minute of the buy", lambda r: None if r["minute"] is None else ("2-5" if r["minute"]<5 else "5-8" if r["minute"]<8 else "8-11" if r["minute"]<11 else "11+"), ["2-5","5-8","8-11","11+"])
tab("by same-side bets the lane had already bought in the window", lambda r: min(r["stack"],3))
tab("by price paid", lambda r: "0.55-0.65" if r["paid"]<0.65 else "0.65-0.75" if r["paid"]<0.75 else "0.75+", ["0.55-0.65","0.65-0.75","0.75+"])
tab("by the model's chance", lambda r: None if r["chance"] is None else ("under 0.55" if r["chance"]<0.55 else "0.55-0.65" if r["chance"]<0.65 else "0.65+"), ["under 0.55","0.55-0.65","0.65+"])
tab("by cushion_fifth", lambda r:r["cush"]); tab("by trend_fifth", lambda r:r["trend"]); tab("by history_fifth", lambda r:r["hist"])
tab("with or against the hour's drift", lambda r:r["with_drift"])
tab("by swing size (u)", lambda r: None if r["swing"] is None else ("under 1.2" if r["swing"]<1.2 else "1.2-2" if r["swing"]<2 else "2+"), ["under 1.2","1.2-2","2+"])
