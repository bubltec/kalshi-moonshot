"""Does a resting buy pay for any variety of favorite? Each favorite (variants.favorites: crypto and commodity, read as a variety at the moment of the buy) is replayed four ways on the tape:
  now       buy at the ask at once (taker fee)
  r1/6+take rest 1c under the ask for 6 s (the live setting), else buy at the next ask if still in band and <= decision ask + 2c
  r2/20     rest 2c under the ask for 20 s, no fallback (a miss is $0)
  wait30    do not buy at once: look again 30 s later and buy at that ask if still in the lane's band (the spot has had 30 s to say where it is going)
By class and variety, with halves, so a pattern that is only in one half shows. Also the ask's own drift 6 s and 30 s on, per variety: where the price goes while a bid would be resting.
Run: scripts/cloud.sh run scripts/rest_variety_replay.py  [DAYS=3]"""
import os, time, collections
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload(); tape = db.connect(s.tape_path, "tape", shared=True)
DAYS = float(os.environ.get("DAYS", "3"))
end = time.time(); start = end - DAYS * 86400
BAND = (0.78, 0.92)

def pnl(price, won, maker):
    c = 10.0 / price
    return ((1 - price) * c if won else -10.0) - (0 if maker else fee(price) * c)

def sask(row, side):
    return row[2] if side == "yes" else 1.0 - row[1]

rows = []
for f in variants.favorites(s, start, end):
    q = [(float(a), float(b), float(c)) for a, b, c in tape.execute(
        "SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (f["ticker"], f["ts"], f["ts"] + 125)).fetchall()]
    side, ask, at, won = f["side"], f["ask"], f["ts"], f["won"]
    close = f["close_ts"]

    def first_after(t):
        r = next((x for x in q if x[0] >= t and x[0] < close - 20), None)
        return sask(r, side) if r else None

    res = {"now": pnl(ask, won, False)}
    # r1/6+take
    rest = round(ask - 0.01, 2)
    fill = next((x for x in q if at < x[0] <= at + 6 and sask(x, side) <= rest + 1e-9), None)
    if fill:
        res["r1/6+take"] = pnl(rest, won, True); kind = "filled"
    else:
        a2 = first_after(at + 6)
        if a2 is not None and BAND[0] - 1e-9 <= a2 <= ask + 0.02 + 1e-9:
            res["r1/6+take"] = pnl(a2, won, False); kind = "took"
        else:
            res["r1/6+take"] = 0.0; kind = "missed"
    # r2/20 rest only
    rest2 = round(ask - 0.02, 2)
    fill2 = next((x for x in q if at < x[0] <= at + 20 and sask(x, side) <= rest2 + 1e-9), None)
    res["r2/20"] = pnl(rest2, won, True) if fill2 else 0.0
    # wait30
    a3 = first_after(at + 30)
    res["wait30"] = pnl(a3, won, False) if a3 is not None and BAND[0] - 1e-9 <= a3 <= 0.95 else 0.0
    d6 = first_after(at + 6); d30 = a3
    rows.append({"cls": f["cls"], "var": f.get("variety") or "?", "close": close, "won": won, "kind": kind, "res": res,
                 "d6": (d6 - ask) if d6 is not None else None, "d30": (d30 - ask) if d30 is not None else None})

mid = sorted(r["close"] for r in rows)[len(rows) // 2] if rows else 0
STRATS = ("now", "r1/6+take", "r2/20", "wait30")

def line(lab, sel):
    n = len(sel)
    if not n: return
    tot = {k: sum(r["res"][k] for r in sel) for k in STRATS}
    h1 = [r for r in sel if r["close"] < mid]; h2 = [r for r in sel if r["close"] >= mid]
    def d(sub, k): return sum(r["res"][k] - r["res"]["now"] for r in sub)
    fm = collections.Counter(r["kind"] for r in sel)
    d6 = [r["d6"] for r in sel if r["d6"] is not None]; d30 = [r["d30"] for r in sel if r["d30"] is not None]
    print(f"{lab:14} n={n:4d} win {100*sum(r['won'] for r in sel)/n:3.0f}% | now {tot['now']:+7.1f} | vs now: r1/6+take {tot['r1/6+take']-tot['now']:+6.1f} (h1 {d(h1,'r1/6+take'):+5.1f} h2 {d(h2,'r1/6+take'):+5.1f})"
          f"  r2/20 {tot['r2/20']-tot['now']:+6.1f} (h1 {d(h1,'r2/20'):+5.1f} h2 {d(h2,'r2/20'):+5.1f})  wait30 {tot['wait30']-tot['now']:+6.1f} (h1 {d(h1,'wait30'):+5.1f} h2 {d(h2,'wait30'):+5.1f})"
          f" | r1 filled {100*fm['filled']/n:.0f}% took {100*fm['took']/n:.0f}% missed {100*fm['missed']/n:.0f}% | ask drift +6s {100*sum(d6)/max(len(d6),1):+.1f}c +30s {100*sum(d30)/max(len(d30),1):+.1f}c")

print(f"##### {len(rows)} favorites in the last {DAYS:g} days; 'vs now' = that plan's $ minus buying at once on the same favorites (positive = the plan is better); $10 tickets, taker fee on the ask, none on rests")
for cls in ("crypto", "commodity"):
    sub = [r for r in rows if r["cls"] == cls]
    line(f"{cls} all", sub)
    for v in sorted({r["var"] for r in sub}):
        line(f"  {cls[:3]} {v}", [r for r in sub if r["var"] == v])
print("DONE")
