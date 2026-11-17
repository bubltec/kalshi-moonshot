import csv, bisect, collections
from kalshi_moonshot import riskscore
from kalshi_moonshot.strategy import fee_per_contract as fee
S="data/tape_archive"  # manual.csv: lane,ticker,ts,side,ask,stake,exit_price of the hand-placed buys, pulled from the host's decisions (lane manual)
N=riskscore.NAMES
by=collections.defaultdict(list)
for r in list(csv.reader(open("data/tape_archive/replay_readings.csv")))[1:]:
    by[r[0]].append((int(r[1]), float(r[2]), int(r[3]), [float(v) for v in r[6:]]))
rows=[]
miss=0
for rec in csv.reader(open(f"{S}/manual.csv")):
    rec=[v.strip() for v in rec]
    if len(rec)!=7: continue
    lane,tk,ts,side,ask,stake,ex=rec
    ts,ask,stake=float(ts),float(ask),float(stake)
    got=by.get(tk)
    if not got: miss+=1; continue
    i=max(0,bisect.bisect_right([g[0] for g in got], ts)-1)
    if abs(got[i][0]-ts)>75: miss+=1; continue
    t,age,yes,x=got[i]
    sg=1 if side=="yes" else -1
    xo=[v if j==8 else sg*v for j,v in enumerate(x)]
    won=(yes==1)==(side=="yes")
    c=stake/ask
    held=c*(1-ask)-c*fee(ask) if won else -stake
    traded=held if ex=="" else c*(float(ex)-ask)-c*fee(max(float(ex),0.01))-c*fee(ask)
    rows.append(dict(age=ts-(t-age),ask=ask,stake=stake,won=won,held=held,traded=traded,x=dict(zip(N,xo)),sold=ex!=""))
print(f"Question: what state were the hand-placed buys made in, and which of those states paid?")
print(f"Window: hand-placed buys on 15-minute markets, 2026-11-09 to 11-16, joined to the replay's reading at the buy (same market, within 75 s): {len(rows)} joined, {miss} with no reading (market not on the replay, or bought on 11-17).")
print("Attributes are turned to the side bought: positive = in the bought side's favor. Dollars as traded and if held, fee on every leg. '$1 a buy' counts every buy the same, so one large stake cannot carry a row. A description of these bets, not a tested rule.\n")
def tab(title, key):
    g=collections.defaultdict(list)
    for r in rows: g[key(r)].append(r)
    print(title)
    for k in sorted(g):
        v=g[k]
        print(f"  {k:28s} buys {len(v):4d} | won {sum(r['won'] for r in v):4d} ({100*sum(r['won'] for r in v)/len(v):3.0f}%) | avg paid {sum(r['ask'] for r in v)/len(v):.2f} | staked ${sum(r['stake'] for r in v):6.0f} | as traded ${sum(r['traded'] for r in v):+8.2f} | if held ${sum(r['held'] for r in v):+8.2f} | $1 a buy, held: {sum(r['held']/r['stake'] for r in v):+7.2f}")
    print()
tab("all", lambda r:"all")
tab("by the cushion for the bought side (index against the target, in typical moves)", lambda r: "1 behind by over 0.5" if r['x']['cushion']<-0.5 else "2 on the line (within 0.5)" if r['x']['cushion']<0.5 else "3 ahead 0.5 to 1.5" if r['x']['cushion']<1.5 else "4 ahead over 1.5")
tab("by minute of the market", lambda r: "1 0-3" if r['age']<180 else "2 3-6" if r['age']<360 else "3 6-9" if r['age']<540 else "4 9-12" if r['age']<720 else "5 12-15")
tab("by the bought side's price move over the last minute (the nudge, in the market's typical minutes)", lambda r: "1 falling (under -0.5)" if r['x']['nudge']<-0.5 else "2 flat" if r['x']['nudge']<0.5 else "3 rising (over 0.5)")
tab("by the index's 5-minute travel toward the bought side", lambda r: (lambda t: "1 away (under -0.5)" if t<-0.5 else "2 flat" if t<0.5 else "3 toward (over 0.5)")(r['x']['trend5 near']+r['x']['trend5 mid']+r['x']['trend5 far']))
tab("by price paid", lambda r: "1 under 0.20" if r['ask']<0.2 else "2 0.20-0.35" if r['ask']<0.35 else "3 0.35-0.50" if r['ask']<0.5 else "4 0.50-0.65" if r['ask']<0.65 else "5 0.65-0.80" if r['ask']<0.8 else "6 0.80 up")
tab("by stake", lambda r: "1 under $5" if r['stake']<5 else "2 $5 to $20" if r['stake']<20 else "3 $20 and over")
