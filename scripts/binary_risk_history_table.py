"""HISTORY vs CUSHION TABLE (2026-11-20, user via parent: "In-market trends/cushion are the most important; historical trends and curve shape decide whether a buy is a good idea, and it must work both ways ... the current model is additive, so it cannot express 'cushion says X but history says not-X'. Test this directly before fitting anything."). Rule 8, state-record style: a table, no fit.
QUESTION: does the realised win rate depend on the in-market cushion AND the history of the spot against the SAME strike, in the way the user describes? One reading per market side: the FIRST reading at least 180 s into the market with at least 180 s left whose ask is in the universe's band. Universes (both from the same commodity perp markets, S&P out, Kalshi perp reference only): FAV = the side asked 0.60-0.85 (the model's band); COUNTER = the cheap side asked 0.15-0.40 (Kalshi favours the other way). Held to settlement; the outcome is the win rate only (no price, no net).
READINGS (the side's view): cushion = the blended 1/3/5-minute average against the strike in the typical minute x sqrt(minutes left) (binaryrisk 'czb', signed to the side); HISTORY SHARE s15/s30/s60 = the share of the last 15/30/60 minutes' ticks the spot spent on the side's WINNING side of the strike (not crossings); and the side's own 30-minute move (mv30) and wave slope (sl30), both in the market's own typical minutes; VOLATILITY = vr (the 15-minute typical minute over the hour's, own-history relative).
BUCKETS: terciles, with the cut points set on the FIRST half of the markets (by close time) only, applied to both halves. Table: cushion x share30 x vr (27 cells) per half, then cushion x share (9 cells) pooled over vr for share 15/30/60, then the disagreement cells. n, win rate, its se; the lift is a cell's win rate minus its universe's own win rate in that half. Nothing here is chosen after the results except where marked.
Run: SPOT=0 scripts/cloud_job.sh scripts/binary_risk_history_table.py"""
import bisect, math, multiprocessing, os, time
from datetime import datetime, timezone
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.strategy import fee_per_contract as fee

t0 = time.time()
START = datetime(2026, 11, 9, 5, tzinfo=timezone.utc).timestamp()
FAV, CTR = (0.60, 0.85), (0.15, 0.40)
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
wins = st.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START + 7200,)).fetchall()
jobs = {}
for w in wins:
    if w["series"].startswith("KXINX"): continue
    ix = perp_index(w["series"])
    if ix: jobs.setdefault(ix, []).append((w["ticker"], float(w["close_ts"]), w["result"] == "yes"))

def build(job):
    ix, ws = job
    tape = db.connect(config.reload().tape_path, "tape", shared=True)
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, min(w[1] for w in ws) - 4900, max(w[1] for w in ws) + 5)).fetchall()
    T, Vv = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    del tk
    out = []
    for ticker, close, won_yes in ws:
        opened = close - 900.0
        k = bisect.bisect_left(T, opened)
        if k >= len(T) or T[k] - opened > 25.0: continue
        strike = Vv[k]
        ref = []
        for hh in (24.0, 72.0):
            rr = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (ix, opened - hh * 3600.0, opened - hh * 3600.0 - 1800.0)).fetchone()
            ref.append(float(rr[0]) if rr else 0.0)
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, opened - 60, close)).fetchall()
        if len(q) < 20: continue
        qt, qb, qa = [float(r[0]) for r in q], [float(r[1]) for r in q], [float(r[2]) for r in q]
        scale = 100.0 if max(qa) > 1.5 else 1.0
        cs = [0]
        for v in Vv: cs.append(cs[-1] + (1 if v > strike else 0))  # prefix count of ticks above the strike
        got = {}
        t = opened + 180.0
        while t <= close - 180.0 and len(got) < 4:
            j = bisect.bisect_right(qt, t) - 1
            if j < 0 or t - qt[j] > 60.0:
                t += 60.0; continue
            yb, ya = qb[j] / scale, qa[j] / scale
            f = br.readings(T, Vv, t, strike, close - t, (ref[0], ref[1]))
            if f:
                je = bisect.bisect_right(T, t) - 1
                sh = {}
                for m in (15, 30, 60):
                    i = bisect.bisect_left(T, t - 60.0 * m)
                    sh[m] = (cs[je + 1] - cs[i]) / max(1, je + 1 - i)  # share of the window above the strike (YES side's winning side)
                for side, ask in (("yes", ya), ("no", 1.0 - yb)):
                    for uni, (lo, hi) in (("FAV", FAV), ("CTR", CTR)):
                        key = (uni, side)
                        if key in got or not lo <= ask <= hi: continue
                        sg = 1.0 if side == "yes" else -1.0
                        won = won_yes if side == "yes" else not won_yes
                        sd = (lambda x: x) if side == "yes" else (lambda x: 1.0 - x)
                        got[key] = (close, ticker, uni, side, ask, 1.0 if won else 0.0, (1.0 if won else 0.0) - ask - fee(ask), sg * f["czb"], sd(sh[15]), sd(sh[30]), sd(sh[60]), sg * f["mv30"], sg * f["sl30"], f["vr"], close - t)
            t += 60.0
        out.extend(got.values())
    return out

with multiprocessing.get_context("fork").Pool(4) as pool:
    R = [r for rs in pool.imap_unordered(build, list(jobs.items())) for r in rs]
R = [(r[:6] + ((r[5] - r[4] - fee(r[4])) / r[4],) + r[7:]) for r in R]  # net per $1
R.sort(key=lambda r: r[0])
print(f"{len(R)} market-side readings ({sum(1 for r in R if r[2]=='FAV')} FAV, {sum(1 for r in R if r[2]=='CTR')} CTR), {time.time()-t0:.0f}s", flush=True)
# row: 0 close,1 ticker,2 uni,3 side,4 ask,5 won,6 net,7 cush,8 s15,9 s30,10 s60,11 mv30,12 sl30,13 vr,14 left
IDX = {"cush": 7, "s15": 8, "s30": 9, "s60": 10, "mv30": 11, "sl30": 12, "vr": 13}

def terc(vals):
    v = sorted(vals)
    return v[len(v) // 3], v[2 * len(v) // 3]
def bucket(x, c): return 0 if x <= c[0] else (1 if x <= c[1] else 2)
def line(rs):
    n = len(rs)
    if n < 5: return f"n {n:4d}  (too few)"
    win = sum(r[5] for r in rs) / n
    se = math.sqrt(max(win * (1 - win), 1e-9) / n)
    return f"n {n:4d} win {win:.2f} se {se:.3f}"
LAB = ("low", "mid", "high")

for uni in ("FAV", "CTR"):
    U = [r for r in R if r[2] == uni]
    mid = len(U) // 2
    H = [U[:mid], U[mid:]]
    cut = {k: terc([r[i] for r in H[0]]) for k, i in IDX.items()}
    print(f"\n######## {uni}: {len(U)} market sides, half 1 {len(H[0])}, half 2 {len(H[1])} (cut points from half 1: " + ", ".join(f"{k} {c[0]:+.2f}/{c[1]:+.2f}" for k, c in cut.items()) + ")")
    print("ALL: h1 " + line(H[0]) + " | h2 " + line(H[1]))
    for sk in ("s30", "s15", "s60"):
        print(f"\n--- cushion x {sk} (pooled over volatility); terciles of the side's cushion and of the share")
        for ci in range(3):
            for si in range(3):
                cells = [[r for r in h if bucket(r[7], cut["cush"]) == ci and bucket(r[IDX[sk]], cut[sk]) == si] for h in H]
                print(f"  cushion {LAB[ci]:4s} {sk} {LAB[si]:4s}: h1 {line(cells[0])} | h2 {line(cells[1])}")
    print("\n--- cushion x s30 x vr (27 cells)")
    for ci in range(3):
        for si in range(3):
            for vi in range(3):
                cells = [[r for r in h if bucket(r[7], cut["cush"]) == ci and bucket(r[9], cut["s30"]) == si and bucket(r[13], cut["vr"]) == vi] for h in H]
                print(f"  cush {LAB[ci]:4s} s30 {LAB[si]:4s} vr {LAB[vi]:4s}: h1 {line(cells[0])} | h2 {line(cells[1])}")
    print("\n--- by the side's own 30-minute move (mv30) and wave slope (sl30) inside the history-against cells (s30 low) and history-for cells (s30 high)")
    for si in (0, 2):
        for mk in ("mv30", "sl30"):
            for mi in range(3):
                cells = [[r for r in h if bucket(r[9], cut["s30"]) == si and bucket(r[IDX[mk]], cut[mk]) == mi] for h in H]
                print(f"  s30 {LAB[si]:4s} {mk} {LAB[mi]:4s}: h1 {line(cells[0])} | h2 {line(cells[1])}")

# ---- the cells as a state-record style JSON (cell key "UNI|c<cushion tercile>|s<share30 tercile>|v<vr tercile>", terciles 0 low .. 2 high; the cut points are the first half's) and a rule map
import json
def agg(rs):
    n = len(rs)
    if n == 0: return {"n": 0, "wins": 0, "win": 0.0, "se": 0.0}
    w = sum(r[5] for r in rs)
    return {"n": n, "wins": int(w), "win": round(w / n, 4), "se": round(math.sqrt(max(w / n * (1 - w / n), 1e-9) / n), 4)}
rec = {"built_at": time.time(), "kind": "history_cushion_cells", "outcome": "win rate to settlement", "band": {"FAV": FAV, "CTR": CTR}, "cuts": {}, "cells": {}}
verdict = {}
for uni in ("FAV", "CTR"):
    U = [r for r in R if r[2] == uni]; mid = len(U) // 2; H = [U[:mid], U[mid:]]
    cut = {k: terc([r[i] for r in H[0]]) for k, i in IDX.items()}
    rec["cuts"][uni] = {k: list(c) for k, c in cut.items()}
    for ci in range(3):
        for si in range(3):
            for vi in range(3):
                cells = [[r for r in h if bucket(r[7], cut["cush"]) == ci and bucket(r[9], cut["s30"]) == si and bucket(r[13], cut["vr"]) == vi] for h in H]
                a1, a2, ap = agg(cells[0]), agg(cells[1]), agg(cells[0] + cells[1])
                key = f"{uni}|c{ci}|s{si}|v{vi}"
                # the rule map (set before reading the cells): BUY = win rate above the universe's own in both halves and the pooled lift at least 1 se; REFUSE = below in both halves; otherwise NO RECORD
                b1, b2 = sum(r[5] for r in H[0]) / len(H[0]), sum(r[5] for r in H[1]) / len(H[1])  # the universe's own win rate in each half
                bp = (b1 * len(H[0]) + b2 * len(H[1])) / len(U)
                big = a1["n"] >= 20 and a2["n"] >= 20
                v = "buy" if big and a1["win"] > b1 and a2["win"] > b2 and ap["se"] and (ap["win"] - bp) / ap["se"] >= 1.0 else ("refuse" if big and a1["win"] < b1 and a2["win"] < b2 else "no_record")
                a1["lift"], a2["lift"], ap["lift"] = round(a1["win"] - b1, 4), round(a2["win"] - b2, 4), round(ap["win"] - bp, 4)
                rec["cells"][key] = {"h1": a1, "h2": a2, "pooled": ap, "verdict": v}
                verdict[key] = v
print("\n=== RULE MAP (BUY = cell win rate above its universe's win rate in both halves, n >= 20 each, pooled lift >= 1 se; REFUSE = below in both halves; else no record)")
for uni in ("FAV", "CTR"):
    for v in ("buy", "refuse"):
        ks = [k for k in rec["cells"] if k.startswith(uni) and verdict[k] == v]
        print(f"{uni} {v.upper()}: {len(ks)} cells")
        for k in ks:
            c = rec["cells"][k]
            print(f"   {k}: h1 n {c['h1']['n']} win {c['h1']['win']:.2f} lift {c['h1']['lift']:+.3f} | h2 n {c['h2']['n']} win {c['h2']['win']:.2f} lift {c['h2']['lift']:+.3f} | pooled n {c['pooled']['n']} win {c['pooled']['win']:.2f} lift {c['pooled']['lift']:+.3f} se {c['pooled']['se']:.3f}")
print("JSON_BEGIN"); print(json.dumps(rec)); print("JSON_END")
print(f"\ndone in {time.time()-t0:.0f}s")
