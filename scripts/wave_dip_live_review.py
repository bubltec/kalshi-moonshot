"""Review of the wave_shape lane's LIVE bets with the wave_dip buy (2026-11-27 01:30 to 07:15 ET, $2 a bet), as round trips: by kind of sale, what the bid did after each sale, the buy's readings, the real fills and fees.
Inputs are three CSV exports from the host (scripts/cloud.sh sql with COPY ... TO STDOUT WITH CSV HEADER; the queries are in studies/results/2026-11-27-wave-dip-live-review.txt): live_bets.csv, live_paths.csv, live_orders.csv in one folder.
    python3 scripts/wave_dip_live_review.py FOLDER"""
import csv, re, sys, statistics as st, collections
SP = sys.argv[1]
def rd(n):
    rows = list(csv.DictReader(l for l in open(f"{SP}/{n}") if l.strip() and not l.startswith("Cannot")))
    return rows
B = rd("live_bets.csv"); P = {r["id"]: r for r in rd("live_paths.csv")}; O = rd("live_orders.csv")
f = lambda x: float(x) if x not in ("", None) else None
bets = []
for r in B:
    p = P.get(r["id"], {})
    b = dict(id=r["id"], ts=f(r["ts"]), tk=r["ticker"], side=r["side"], ask=f(r["ask"]), bid0=f(r["bid"]), stake=f(r["stake_usd"]), xp=f(r["exit_price"]), xts=f(r["exit_ts"]), out=r["outcome"], how=r["how"] or ("settled" if r["outcome"] else "open"),
             peak_held=f(p.get("peak_held")), low_held=f(p.get("low_held")), peak_after=f(p.get("peak_after")), peak_2m=f(p.get("peak_2m")), secs_after=f(p.get("secs_after")), to_close=f(p.get("secs_to_close")))
    m = re.search(r"swing ([\d.]+) typical minutes .*?the swing before it ([\d.]+), gave back ([-\d.]+); the big leg has run ([\d.n/a]+) of a usual one and travelled ([\d.n/a]+) of a usual wave( \(at its extreme\))?; the target is ([\d.]+)", r["rd"] or "")
    if m:
        b.update(swing=float(m[1]), prev=float(m[2]), age=None if "n" in m[4] else float(m[4]), travel=None if "n" in m[5] else float(m[5]), extreme=bool(m[6]), reach=float(m[7]))
    b["n"] = b["stake"] / b["ask"]
    won = b["out"] == b["side"]
    end = b["xp"] if b["xts"] else (1.0 if won else 0.0) if b["out"] else None
    b["c"] = None if end is None else (end - b["ask"]) * 100
    b["g"] = None if end is None else b["n"] * (end - b["ask"])
    b["won"] = won
    bets.append(b)
L = [b for b in bets if b["stake"] <= 3 and b["c"] is not None]
print(f"the lane's own bets (stake <= $3, closed): {len(L)}; markets {len({b['tk'] for b in L})}; trips per market {len(L)/len({b['tk'] for b in L}):.2f}")
def line(nm, g, extra=""):
    if not g: return
    w = [b for b in g if b["g"] > 0]; lo = [b for b in g if b["g"] <= 0]
    print(f"  {nm:34s} n={len(g):3d} wins {len(w):3d} ({len(w)/len(g):4.0%}) {st.mean(b['c'] for b in w) if w else 0:+5.1f}c | losses {len(lo):3d} {st.mean(b['c'] for b in lo) if lo else 0:+6.1f}c | gross {sum(b['g'] for b in g):+7.2f}{extra}")
line("ALL", L)
print("\n1. BY KIND OF SALE, and what the bid did AFTER the sale (peak of the side's bid from the sale to the close)")
for how, n in collections.Counter(b["how"] for b in L).most_common():
    g = [b for b in L if b["how"] == how]
    s = [b for b in g if b["xts"] and b["peak_after"] is not None]
    ex = ""
    if s:
        back = [b for b in s if b["peak_after"] > b["ask"] + 0.03]
        ex = (f" | sold at {st.mean(b['xp'] for b in s):.2f} (paid {st.mean(b['ask'] for b in s):.2f}), held {st.median(b['xts']-b['ts'] for b in s):.0f}s, {st.median(b['secs_after'] for b in s):.0f}s left; later bid peak median {st.median(b['peak_after'] for b in s):.2f}; "
              f"bid later over cost+3c on {len(back)} ({len(back)/len(s):.0%}); within 2 min on {sum(1 for b in s if (b['peak_2m'] or 0) > b['ask']+0.03)}; settled a win {sum(b['won'] for b in s)}")
    line(how, g, ex)
gu = [b for b in L if b["how"] == "wave give-up" and b["peak_after"] is not None]
print(f"\n   give-ups: sold at {st.mean(b['xp'] for b in gu):.2f}; the bid's LOW while held {st.mean(b['low_held'] for b in gu):.2f}; its peak while held {st.mean(b['peak_held'] for b in gu):.2f} (paid {st.mean(b['ask'] for b in gu):.2f}); ever ahead of cost+3c while held: {sum(1 for b in gu if b['peak_held'] > b['ask']+0.03)}")
print(f"   if every give-up had instead been sold at its later peak bid: {sum(b['n']*(b['peak_after']-b['ask']) for b in gu):+.2f} (a ceiling: nobody sells the peak) against {sum(b['g'] for b in gu):+.2f} as sold")
ts_ = [b for b in L if b["how"] == "turn sale" and b["peak_after"] is not None]
print(f"   turn sales: sold at {st.mean(b['xp'] for b in ts_):.2f}, later peak median {st.median(b['peak_after'] for b in ts_):.2f}; the bid went on 10c or more past the sale on {sum(1 for b in ts_ if b['peak_after'] >= b['xp']+0.10)} of {len(ts_)}; fell back under cost later on {sum(1 for b in ts_ if not b['won'])} (settled a loss)")
print("\n2. WHAT WAS ON OFFER WHILE HELD OR AFTER (peak bid from the buy to the close, all closed bets)")
pk = [max(x for x in (b["peak_held"], b["peak_after"]) if x is not None) for b in L if b["peak_held"] is not None]
aa = [b for b in L if b["peak_held"] is not None]
for lv in (0.5, 0.6, 0.7, 0.8):
    print(f"   bid reached {lv:.2f}: {sum(1 for x in pk if x >= lv)/len(pk):.0%}", end="")
print(f"\n   peak bid while HELD at least cost+5c: {sum(1 for b in aa if b['peak_held'] >= b['ask']+0.05)/len(aa):.0%}; +10c: {sum(1 for b in aa if b['peak_held'] >= b['ask']+0.10)/len(aa):.0%}; +20c: {sum(1 for b in aa if b['peak_held'] >= b['ask']+0.20)/len(aa):.0%}")
print("\n3. THE BUY'S READINGS, winners against losers (thirds on the lane's bets)")
def thirds(nm, fn):
    S = [b for b in L if fn(b) is not None]
    v = sorted(fn(b) for b in S); a, c = v[len(v)//3], v[2*len(v)//3]
    print(f" {nm} (cuts {a:.2f}, {c:.2f})")
    line("low", [b for b in S if fn(b) <= a]); line("mid", [b for b in S if a < fn(b) <= c]); line("high", [b for b in S if fn(b) > c])
thirds("price paid", lambda b: b["ask"])
thirds("minutes into the market", lambda b: 15 - (b["to_close"] or 0)/60 if b["to_close"] else None)
for k, nm in (("swing", "the dip or peak's size at the buy, typical minutes"), ("prev", "the swing before it"), ("age", "big leg run / usual"), ("travel", "leg travel / usual wave"), ("reach", "target distance / usual travel for the time left")):
    thirds(nm, lambda b, k=k: b.get(k))
print(" at its extreme"); line("yes", [b for b in L if b.get("extreme")]); line("no", [b for b in L if b.get("extreme") is False])
print(" side"); line("YES (dip)", [b for b in L if b["side"] == "yes"]); line("NO (peak)", [b for b in L if b["side"] == "no"])
print(" by the bet's order in its market"); 
seen = collections.Counter()
for b in sorted(L, key=lambda b: b["ts"]):
    seen[b["tk"]] += 1; b["nth"] = seen[b["tk"]]
for k in (1, 2, 3): line(f"{k}{'st' if k==1 else 'nd' if k==2 else 'rd+'} trip", [b for b in L if (b["nth"] == k if k < 3 else b["nth"] >= 3)])
print("\n4. THE ORDERS (every order on these markets in the window, PriskP's included)")
buys = [o for o in O if o["side"] == "buy"]; sells = [o for o in O if o["side"] == "sell"]
fb = [o for o in buys if float(o["fill_count"] or 0) > 0]; fs = [o for o in sells if float(o["fill_count"] or 0) > 0]
print(f"   buy orders {len(buys)}, filled {len(fb)} ({len(fb)/len(buys):.0%}); with a fee of zero {sum(1 for o in fb if float(o['fee_usd'] or 0) == 0)}; fees paid on buys ${sum(float(o['fee_usd'] or 0) for o in fb):.2f}")
print(f"   sell orders {len(sells)}, filled {len(fs)}; with a fee of zero {sum(1 for o in fs if float(o['fee_usd'] or 0) == 0)}; fees paid on sells ${sum(float(o['fee_usd'] or 0) for o in fs):.2f}")
big = [b for b in bets if b["stake"] > 3]
print("\n5. THE OVERSIZED ROWS:", [(b["tk"].split("-")[0], b["side"], b["ask"], round(b["stake"],2), b["xp"], b["how"]) for b in big])
