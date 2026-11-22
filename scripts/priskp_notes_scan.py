"""Evidence scan for the PriskP market notes (2026-11-22; user: generate the notation list from the feedback given and from the run's buys).
Each CANDIDATE NOTE is a condition on what was known BEFORE the buy (the checks' readings, the wave shape, the past-hour profile), taken from the user's own reviews (named in the source column of the draft), stated before this scan. For each: the model's buys where it holds, net per $1, share ahead against price paid plus fee, and whether the sign agrees in both halves of the days. Numeric conditions use the pooled upper or lower third of the model's buys (DISCOVERY cuts, not the own-history cuts of the live checks; said so). Descriptive: a lead, never a rule. Input: priskp_buys_crypto.json.gz (scripts/priskp_run.py). Run: python3 scripts/priskp_notes_scan.py priskp_buys_crypto.json.gz"""
import gzip, json, sys
fee = lambda p: 0.07 * p * (1.0 - p)
rows = [r for r in json.load(gzip.open(sys.argv[1])) if r["rule"] == "model" and r.get("hr")]
half = sorted(r["close"] for r in rows)[len(rows) // 2]


def third(fn, top=True):
    xs = sorted(fn(r) for r in rows)
    c = xs[int(len(xs) * (2 / 3 if top else 1 / 3))]
    return (lambda r: fn(r) >= c) if top else (lambda r: fn(r) <= c), c


def cell(g):
    n = len(g)
    if n < 30: return None
    w = sum(r["won"] for r in g) / n
    return n, 100.0 * sum(r["net"] for r in g) / (10.0 * n), w, sum(r["price"] + fee(r["price"]) for r in g) / n


def show(name, cond):
    g = [r for r in rows if cond(r)]; rest = [r for r in rows if not cond(r)]
    a, b, o = cell(g), cell([r for r in g if r["close"] < half]), cell([r for r in g if r["close"] >= half])
    c = cell(rest)
    if not a: print(f"{name:58s} n {len(g)} (too few)"); return
    agree = "both halves " + ("+" if b and o and b[1] > 0 and o[1] > 0 else "-" if b and o and b[1] < 0 and o[1] < 0 else "mixed") if b and o else "halves too few"
    print(f"{name:58s} n {a[0]:5d} {a[1]:+5.1f}c ahead {100*a[2]:.0f}% need {100*a[3]:.0f}% {'BEAT' if a[2] > a[3] else 'no  '} | {agree:18s} | rest {c[1]:+5.1f}c" if c else "")


H = lambda r: r["hr"]
S = lambda r, k: r["states"].get(k)
conds = []
f, c = third(lambda r: r["raw"]["cushion"], top=False); conds.append((f"thin_cushion: cushion in the lowest third (<= {c:+.2f})", f))
f, c = third(lambda r: H(r)["cross"]); conds.append((f"chop: strike crossed >= {c} times in the hour", f))
f, c = third(lambda r: H(r)["range"]); conds.append((f"wide_range: hour range >= {c:.1f} typical moves", f))
f, c = third(lambda r: H(r)["eff"], top=False); conds.append((f"choppy_path: path efficiency <= {c:.3f}", f))
conds.append(("at_hour_high: the hour's high was within the last 5 min", lambda r: H(r)["hi_ago"] <= 5))
conds.append(("off_hour_low: the hour's low was within the last 5 min", lambda r: H(r)["lo_ago"] <= 5))
f, c = third(lambda r: H(r)["drift"], top=False); conds.append((f"against_hour: hour drift against the side (<= {c:+.1f})", f))
conds.append(("lost_recently: last 10-min block under 70% on the winning side", lambda r: H(r)["blocks"][5] < 0.70))
conds.append(("spike_peak: 15-min move fifth 5 and price dip fifth 5", lambda r: S(r, "f:mv15") == 5 and S(r, "pricedip") == 5))
conds.append(("counter_trend_spike: 15-min up, 30 and 60-min slopes not rising", lambda r: S(r, "f:mv15") in (4, 5) and S(r, "t:sl30") in (1, 2) and S(r, "t:sl60") in (1, 2)))
f, c = third(lambda r: sum(H(r)["bins"][6:]) / 6 - sum(H(r)["bins"][:6]) / 6, top=False); conds.append((f"shift_down: last-30-min level minus first-30 <= {c:+.1f}", f))
conds.append(("early: bought before minute 4", lambda r: r["minute"] < 4))
conds.append(("early_thin: before minute 4 and cushion in the lowest third", lambda r: r["minute"] < 4 and conds[0][1](r)))
conds.append(("early_with_history: before minute 4 and history fifth 5", lambda r: r["minute"] < 4 and S(r, "history") == 5))
conds.append(("late_agree: from minute 10 and all five checks in fifths 4-5 with cushion fifth 5", lambda r: r["minute"] >= 10 and S(r, "cushion") == 5))
conds.append(("falling_30_60_bending_up: 30 and 60-min slope tercile 1, 30-min curvature 3", lambda r: S(r, "t:sl30") == 1 and S(r, "t:sl60") == 1 and S(r, "t:cv30") == 3))
print(f"{len(rows)} model buys with an hour profile; model overall {cell(rows)[1]:+.1f}c\n")
for n, f in conds: show(n, f)
