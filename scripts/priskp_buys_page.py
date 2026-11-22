"""One self-contained page of the PriskP checks model's buys on the recorded tape (2026-11-22, user via parent: "send me links to the buys that won and lost so we can evaluate").
Input: priskp_buys_crypto.json.gz written by scripts/priskp_run.py (the model's rules and every bound are listed in that file's header and in the page's header table). Output: one HTML file, no outside scripts.
Run: python3 scripts/priskp_buys_page.py priskp_buys_crypto.json.gz studies/results/2026-11-22-priskp-crypto-buys.html"""
import gzip, json, sys
fee = lambda p: 0.07 * p * (1.0 - p)
rows = json.load(gzip.open(sys.argv[1]))
model = [r for r in rows if r["rule"] == "model"]
plain = [r for r in rows if r["rule"] == "plain"]
mk = {r["ticker"] for r in model}
for r in model + plain:
    s = r["states"]
    sl, cv = s.get("t:sl30"), s.get("t:cv30")
    r["seg"] = "" if sl in (None, "unread", "anomaly") or cv in (None, "unread", "anomaly") else f"{['falling', 'flat', 'rising'][sl - 1]} 30 min, {['bending down', 'straight', 'bending up'][cv - 1]}"
refused = [r for r in plain if r["ticker"] not in mk]


def stats(g):
    n = len(g)
    if n == 0: return None
    win = sum(1 for r in g if r["won"]) / n
    return {"n": n, "net": round(sum(r["net"] for r in g), 2), "c": round(100.0 * sum(r["net"] for r in g) / (10.0 * n), 1), "ahead": round(100 * win, 1), "paid": round(sum(r["price"] for r in g) / n, 3), "need": round(100 * sum(r["price"] + fee(r["price"]) for r in g) / n, 1), "beat": win > sum(r["price"] + fee(r["price"]) for r in g) / n}


BANDS = ((2, 5), (5, 10), (10, 12), (12, 14))
segs = sorted({r["seg"] for r in plain if r["seg"]})
PB = ((0.0, 0.20), (0.20, 0.45), (0.55, 0.65), (0.65, 0.85), (0.85, 1.0))
summary = {"bands": {f"{a}-{b}": {"model": stats([r for r in model if a <= r["minute"] < b + (0.001 if b == 14 else 0)]), "plain": stats([r for r in plain if a <= r["minute"] < b + (0.001 if b == 14 else 0)])} for a, b in BANDS},
           "pbands": {f"{a:.2f}-{b:.2f}": {"model": stats([r for r in model if a <= r["price"] < b]), "plain": stats([r for r in plain if a <= r["price"] < b])} for a, b in PB}, "model": stats(model), "plain": stats(plain), "refused": stats(refused), "seg": {s: {"model": stats([r for r in model if r["seg"] == s]), "plain": stats([r for r in plain if r["seg"] == s])} for s in segs}}
CH = ("cushion", "trend", "history", "price", "pricedip")
FEAT = set()
def slim(r, full):
    o = {"t": r["ticker"], "sd": r["side"], "p": round(r["price"], 3), "m": round(r["minute"], 2), "cl": r["close"], "n": round(r["net"], 2), "w": r["won"], "st": {k: r["states"].get(k) for k in CH + ("volatility",)}, "rd": {k: (None if r["raw"].get(k) is None else round(r["raw"][k], 3)) for k in CH + ("volatility",)}, "sg": r["seg"]}
    if full:
        o["sh"] = {k: round(r["raw"][k], 2) for k in ("t:sl15", "t:sl30", "t:sl60", "t:cv15", "t:cv30", "t:cv60", "t:ft15", "t:ft30", "t:ft60") if r["raw"].get(k) is not None}
        o["ts"] = {k: r["states"].get(k) for k in ("t:sl15", "t:sl30", "t:sl60", "t:cv15", "t:cv30", "t:cv60")}
        if r["ticker"] in FEAT: o["hr"] = r.get("hr"); o["ch"] = r.get("chart")
    return o
ranked0 = sorted(model, key=lambda r: r["net"])
GROUPS = {"all": lambda r: True, "fav": lambda r: r["price"] >= 0.65, "fav85": lambda r: r["price"] >= 0.85, "mid": lambda r: 0.65 <= r["price"] < 0.85, "low": lambda r: r["price"] < 0.65}
for gname, gf in GROUPS.items():  # the biggest losers and winners of each price group carry a chart: dollars are driven by price, so the all-prices lists show the cheapest buys
    gr = [r for r in ranked0 if gf(r)]
    FEAT.update(r["ticker"] for r in gr[:60] + gr[-60:])
FEAT.update(r["ticker"] for r in ranked0[:100] + ranked0[-100:])  # charts and hour profiles for the 150 biggest losers and 150 biggest winners (a page of every chart would pass 16 MB)
data = {"model": [slim(r, True) for r in model], "refused": [slim(r, False) for r in refused], "summary": summary, "segs": segs}
ranked = sorted(model, key=lambda r: r["net"])
data["lists"] = {g: {"losers": [r["ticker"] for r in [x for x in ranked if f(x)][:30]], "winners": [r["ticker"] for r in [x for x in ranked[::-1] if f(x)][:30]]} for g, f in GROUPS.items()}
html = open(__file__.replace("priskp_buys_page.py", "priskp_buys_page.html")).read().replace("/*DATA*/null", json.dumps(data, separators=(",", ":")))
open(sys.argv[2], "w").write(html)
print(f"{len(model)} model buys, {len(plain)} plain buys, {len(refused)} refused; page {len(html) / 1e6:.1f} MB")
