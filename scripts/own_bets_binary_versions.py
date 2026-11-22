"""The binary risk lanes' own bets, split by the model version that placed them (the note's form says which): the OLD version (one cushion on the last tick, no gate) and the version since 2026-11-21 09:00 ET (the 1/3/5-minute cushion vector, the gate: every cushion over 0 and 3 minutes left). Reads own_bets_binary.json.gz (scripts/own_bets_binary_check.py) and own_bets.json.gz (scripts/own_bets_dump.py).
Reported: per lane and version n, dollars, ahead; how many bets had a cushion against their side, were bought in the first 2 minutes or with under 3 left; whether the old note's cushion recomputes (against the last tick, czt); and what today's gate would have passed and refused among the old version's bets.
    python3 scripts/own_bets_binary_versions.py"""
import glob, gzip, json, re, statistics as st

load = lambda n: json.load(gzip.open(sorted(glob.glob(f"job-results/*/files/{n}.json.gz"))[-1]))
N = load("own_bets_binary")
R = {(r["ticker"], round(r["ts"], 3), r["lane"]): r for r in load("own_bets")}
rows = []
for x in N:
    r = R.get((x["ticker"], round(x["ts"], 3), x["lane"]))
    if not r: continue
    p = re.search(r"binary_risk yes \((binary risk .*?)\)\)", r["reason"]).group(1)
    old = re.search(r"cushion ([-+\d.]+), efficiency ([\d.]+), hour place ([\d.]+)", p)
    x["ver"] = "new (cushion vector, gate)" if "1/3/5 min" in p else "old (single cushion)" if old else "other"
    x["oldc"] = float(old.group(1)) if old else None
    rows.append(x)
f = lambda h: "n %3d net %+8.2f ahead %2.0f%%" % (len(h), sum(x["net"] for x in h), 100 * sum(x["net"] > 0 for x in h) / max(1, len(h)))
gate = lambda x: x["new"]["cz1"] > 0 and x["new"]["cz3"] > 0 and x["new"]["cz5"] > 0 and x["new"]["left"] >= 3.0
for lane in ("crypto_risk_score", "crypto_binary_risk"):
    for v in ("old (single cushion)", "new (cushion vector, gate)", "other"):
        g = [x for x in rows if x["lane"] == lane and x["ver"] == v]
        if g: print("%-18s %-27s %s | a 1, 3 or 5-minute cushion against the side: %3d | under 3 min left: %3d | first 2 min: %3d" % (lane, v, f(g), sum(min(x["new"]["cz1"], x["new"]["cz3"], x["new"]["cz5"]) < 0 for x in g), sum(x["new"]["left"] < 3 for x in g), sum(x["minute"] < 2 for x in g)))
old = [x for x in rows if x["oldc"] is not None]
for k in ("czt", "cz3", "czb"):
    d = sorted(abs(x["oldc"] - x["new"][k]) for x in old)
    print("old note's cushion against the recomputed %-3s: within 0.1 on %3d of %d, median gap %.2f" % (k, sum(e <= 0.1 for e in d), len(d), d[len(d) // 2]))
print("old note's cushion: median %+.2f, at or under +0.2 on %.0f%%, negative on %d" % (st.median(x["oldc"] for x in old), 100 * sum(x["oldc"] <= 0.2 for x in old) / len(old), sum(x["oldc"] < 0 for x in old)))
for lane in ("crypto_risk_score", "crypto_binary_risk"):
    g = [x for x in old if x["lane"] == lane]
    print(lane, "old-version bets | today's gate would pass:", f([x for x in g if gate(x)]), "| refuse:", f([x for x in g if not gate(x)]))
