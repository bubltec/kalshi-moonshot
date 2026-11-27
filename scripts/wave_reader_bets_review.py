"""Every bet of the wave lane's own reader (wave_turn), live and paper, as round trips: by spike size, metals against coins (a label for the report only), price band and kind of sale (2026-11-27; user: "That's just one example and it's done dozens of live bets since we turned it on").
Input: a CSV export from the host (scripts/cloud.sh sql, COPY ... TO STDOUT WITH CSV HEADER): decisions of lane wave_shape with 'wave turn (fired)' in the reason since 2026-11-25 19:00 ET, columns id, ts, mode, ticker, side, ask, stake_usd,
exit_price, exit_ts, outcome, how (the sale's kind), sw (the reader's "swing N u ..." words), legs ("legs: median N s and N u").
    python3 scripts/wave_reader_bets_review.py reader_bets.csv
A SPIKE here is the swing the reader bought against as a multiple of the market's own median leg (both in the note, the market's own units); 1.5 is a cut chosen after seeing the result."""
import csv
import re
import statistics as st
import sys

METALS = ("KXGOLD", "KXSILVER", "KXCOPPER", "KXPLATINUM", "KXPALLADIUM", "KXWTI", "KXBRENT", "KXNATGAS")


def load(path):
    rows = []
    for r in csv.DictReader(ln for ln in open(path) if ln.strip() and not ln.startswith("Cannot")):
        a, stake = float(r["ask"]), float(r["stake_usd"] or 0)
        if a <= 0 or stake <= 0 or stake > 6:
            continue
        end = float(r["exit_price"]) if r["exit_ts"] else ((1.0 if r["outcome"] == r["side"] else 0.0) if r["outcome"] else None)
        m, leg = re.search(r"swing ([\d.]+)u", r["sw"] or ""), re.search(r"and ([\d.]+)u", r["legs"] or "")
        if end is None or not m or not leg:
            continue
        rows.append({"mode": r["mode"], "metal": r["ticker"].startswith(METALS), "c": (end - a) * 100, "g": stake / a * (end - a), "how": (r["how"] or "settled").split(" filled at")[0], "ratio": float(m[1]) / float(leg[1]), "ask": a})
    return rows


def line(nm, g):
    if not g:
        return
    w = [x for x in g if x["g"] > 0]; lo = [x for x in g if x["g"] <= 0]
    gu = [x for x in g if x["how"] == "wave give-up"]
    print(f"  {nm:38s} n={len(g):3d} wins {len(w) / len(g):4.0%} {st.mean(x['c'] for x in w) if w else 0:+5.1f}c | losses {st.mean(x['c'] for x in lo) if lo else 0:+6.1f}c | gross {sum(x['g'] for x in g):+8.2f} | give-ups {len(gu):3d} ({sum(x['g'] for x in gu):+.2f})")


if __name__ == "__main__":
    R = load(sys.argv[1])
    print(f"{len(R)} closed bets of the lane's own reader: live {sum(x['mode'] == 'trade' for x in R)}, paper {sum(x['mode'] != 'trade' for x in R)}")
    line("ALL", R); line("live", [x for x in R if x["mode"] == "trade"]); line("paper", [x for x in R if x["mode"] != "trade"])
    line("metals", [x for x in R if x["metal"]]); line("coins", [x for x in R if not x["metal"]])
    for nm, f in (("metals", True), ("coins", False)):
        G = [x for x in R if x["metal"] == f]
        print(f"    {nm}: swing / usual leg median {st.median(x['ratio'] for x in G):.2f}; 1.5 x or more on {sum(x['ratio'] >= 1.5 for x in G) / len(G):.0%}")
    print(" the swing bought against / the market's own median leg")
    for a, b in ((0, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 99)):
        line(f"{a:g} to {b:g}" if b < 99 else f"over {a:g}", [x for x in R if a <= x["ratio"] < b])
    band = [x for x in R if 0.18 <= x["ask"] <= 0.40]; out = [x for x in R if not 0.18 <= x["ask"] <= 0.40]
    print(" inside the 0.18-0.40 band"); line("all", band); line("swing under 1.5 x", [x for x in band if x["ratio"] < 1.5]); line("swing 1.5 x or more (a spike)", [x for x in band if x["ratio"] >= 1.5])
    print(" outside the band"); line("all", out); line("swing under 1.5 x", [x for x in out if x["ratio"] < 1.5]); line("swing 1.5 x or more", [x for x in out if x["ratio"] >= 1.5])
    print(" by kind of sale")
    for how in sorted({x["how"] for x in R}, key=lambda h: -sum(1 for x in R if x["how"] == h)):
        line(how, [x for x in R if x["how"] == how])
