"""Who jumps first in an "all coins lose" wave. (2026-11-19, user: "who jumps first")

One question on the same markets as crypto_wave_shapes.py (all eleven crypto series, archived Kalshi quotes, the lane's band 0.60 to 0.85, a set = four or more coins on one way at a 60 s mark with 4 to 13 minutes left, the first such mark in a window and way): inside a set, does the ORDER the coins jumped in say anything? Each coin's jump time is the second, in the 5 minutes before the mark, with the biggest 30 s rise of its side mid. Coins are ranked by it: the earlier half are LEADERS, the later half LAGGERS (ranks, not names). Readings use Kalshi's own quotes only.
Reported, all held to settlement with the fee 0.07 p (1 - p) on every leg, net per $100 staked, and how often a coin's side lost:
 1. leaders and laggers bought at the mark: n coins, mean ask, share lost, net;
 2. the same by spread of the set's jump times (tight = they jumped together, wide = in turn), by thirds;
 3. what a leader's next minute says: at the mark plus 60 s, the leaders' mean side move over that minute (up or down), and the net of buying the laggers (and all the set) then at their ask that moment, by that move;
 4. the share of sets that went 'all lose' (75% or more of the set's coins lost) by the same leader move.
The cuts (halves, thirds, the sign of the leaders' move) are a study's, chosen before the results; none is a live rule. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_wave_lead_lag.py > studies/results/<date>-crypto-wave-lead-lag.txt"""
import csv
import glob
import math
from collections import defaultdict

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

LO, HI = 0.60, 0.85
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
MARKS = list(range(780, 230, -60))
OFFS = list(range(-300, 301, 60))
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
events = []
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    day = f.split("day=")[1][:10]
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas()
    df = df[df["ticker"].str.split("-").str[0].isin(SERIES)].sort_values(["ticker", "ts"])
    wins = defaultdict(dict)  # close -> ticker -> arrays per second (mid, bid, ask, d2 prefix)
    for ticker, g in df.groupby("ticker", sort=False):
        if ticker not in res or len(g) < 120:
            continue
        close, result = res[ticker]
        ts = g["ts"].to_numpy()
        yb, ya = g["bid"].to_numpy(), g["ask"].to_numpy()
        ok = (yb > 0) & (ya > 0) & (ya <= 1) & (ya >= yb) & (ts < close)
        ts, yb, ya = ts[ok], yb[ok], ya[ok]
        if len(ts) < 120 or ts[0] > close - 700:
            continue
        mid = (yb + ya) / 2.0
        secs = np.arange(close - 900, close)
        at = np.searchsorted(ts, secs, side="right") - 1
        have = at >= 0
        msec = np.where(have, mid[np.maximum(at, 0)], np.nan)
        asec = np.where(have, ya[np.maximum(at, 0)], np.nan)
        bsec = np.where(have, yb[np.maximum(at, 0)], np.nan)
        d = np.where(have[1:] & have[:-1], np.diff(msec), np.nan)
        wins[close][ticker] = {"m": msec, "a": asec, "b": bsec, "d2": d * d, "result": result}
    for close, tk in wins.items():
        if len(tk) < 6:
            continue
        for way in ("yes", "no"):
            for left in MARKS:
                k = 900 - left
                if k < 300:
                    continue
                coins = []
                for ticker, a in tk.items():
                    ask = a["a"][k] if way == "yes" else 1.0 - a["b"][k]
                    if np.isnan(ask) or not (LO <= ask <= HI):
                        continue
                    seen = a["d2"][:k][~np.isnan(a["d2"][:k])]
                    if len(seen) < 120:
                        continue
                    unit = float(np.sqrt(np.mean(seen)) * math.sqrt(60.0))
                    if unit <= 0:
                        continue
                    side = a["m"] if way == "yes" else 1.0 - a["m"]
                    if np.isnan(side[k - 300:k + 1]).any():
                        continue
                    coins.append((ticker, a, side, unit))
                if len(coins) < 4:
                    continue
                jt = []
                for _, _, s, _ in coins:
                    w = s[k - 300:k + 1]
                    rise = w[30:] - w[:-30]
                    jt.append(int(np.argmax(rise)))  # seconds from the window start (older = smaller)
                order = np.argsort(jt, kind="stable")
                half = len(coins) // 2
                lead_idx, lag_idx = set(order[:half].tolist()), set(order[len(coins) - half:].tolist())
                rows = []
                for i, (_, a, s, _) in enumerate(coins):
                    way_win = a["result"] == way
                    ask0 = float(a["a"][k]) if way == "yes" else 1.0 - float(a["b"][k])
                    ask1 = float(a["a"][k + 60]) if way == "yes" else 1.0 - float(a["b"][k + 60])
                    rows.append({"lead": i in lead_idx, "lag": i in lag_idx, "win": way_win, "a0": ask0, "a1": ask1, "move": float(s[k + 60] - s[k]) if k + 60 < 900 else float("nan")})
                lost = [not r["win"] for r in rows]
                events.append({"rows": rows, "n": len(coins), "lose": float(np.mean(lost)), "spread": float(np.std(jt)), "left": left})
                break  # the first mark where the set reaches four counts: one a window and way
n = len(events)
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {n} favourite sets (one a window and way), band {LO:.2f} to {HI:.2f}, 4 to 13 minutes left")


def net(rows, key):
    cost = pay = 0.0
    for r in rows:
        p = r[key]
        if not (0.01 < p < 0.99) or math.isnan(p):
            continue
        cost += p + 0.07 * p * (1.0 - p)
        pay += 1.0 if r["win"] else 0.0
    return (100.0 * (pay - cost) / cost if cost else float("nan")), cost


def line(name, rows, key):
    rr = [r for r in rows if 0.01 < r[key] < 0.99 and not math.isnan(r[key])]
    if not rr:
        return
    nt, _ = net(rr, key)
    print(f"  {name:34s} coins {len(rr):5d} | mean ask {np.mean([r[key] for r in rr]):.3f} | share lost {100.0 * np.mean([not r['win'] for r in rr]):5.1f}% | net per $100 {nt:+6.1f}")


allrows = [r for e in events for r in e["rows"]]
print("--- 1. at the mark: leaders (earlier half to jump) and laggers (later half)")
line("all coins in sets", allrows, "a0")
line("leaders", [r for r in allrows if r["lead"]], "a0")
line("laggers", [r for r in allrows if r["lag"]], "a0")
print("--- 2. by the spread of the set's jump times (seconds, std), thirds")
cuts = np.quantile([e["spread"] for e in events], [1 / 3, 2 / 3])
for nm, lo, hi in (("tight", -1, cuts[0]), ("middle", cuts[0], cuts[1]), ("wide", cuts[1], 1e9)):
    sel = [e for e in events if lo < e["spread"] <= hi]
    rows = [r for e in sel for r in e["rows"]]
    print(f"  {nm} (spread {lo if lo > 0 else 0:.0f} to {min(hi, 999):.0f} s): sets {len(sel)}, all lose {100.0 * np.mean([e['lose'] >= 0.75 for e in sel]):.1f}%")
    line("  leaders", [r for r in rows if r["lead"]], "a0")
    line("  laggers", [r for r in rows if r["lag"]], "a0")
print("--- 3. a minute after the mark: leaders' mean side move over the minute, then buy at that moment's ask")
for nm, test in (("leaders fell (move < 0)", lambda m: m < 0), ("leaders flat or rose (move >= 0)", lambda m: m >= 0)):
    sel = []
    for e in events:
        lm = [r["move"] for r in e["rows"] if r["lead"] and not math.isnan(r["move"])]
        if lm and test(float(np.mean(lm))):
            sel.append(e)
    rows = [r for e in sel for r in e["rows"]]
    print(f"  {nm}: sets {len(sel)}, all lose {100.0 * np.mean([e['lose'] >= 0.75 for e in sel]) if sel else 0:.1f}%, mean share lost {100.0 * np.mean([e['lose'] for e in sel]) if sel else 0:.1f}%")
    line("  laggers at the mark + 60 s", [r for r in rows if r["lag"]], "a1")
    line("  leaders at the mark + 60 s", [r for r in rows if r["lead"]], "a1")
    line("  whole set at the mark + 60 s", rows, "a1")
    line("  laggers at the mark", [r for r in rows if r["lag"]], "a0")
print("--- 4. cut of the leaders' move into thirds (known at the mark + 60 s)")
mv = [(float(np.mean([r["move"] for r in e["rows"] if r["lead"] and not math.isnan(r["move"])] or [float("nan")])), e) for e in events]
mv = [(m, e) for m, e in mv if not math.isnan(m)]
c = np.quantile([m for m, _ in mv], [1 / 3, 2 / 3])
for nm, lo, hi in (("leaders fell most", -9, c[0]), ("middle", c[0], c[1]), ("leaders rose most", c[1], 9)):
    sel = [e for m, e in mv if lo < m <= hi] if lo > -9 else [e for m, e in mv if m <= hi]
    rows = [r for e in sel for r in e["rows"]]
    print(f"  {nm} (move up to {hi:+.3f}): sets {len(sel)}, all lose {100.0 * np.mean([e['lose'] >= 0.75 for e in sel]):.1f}%")
    line("  laggers at the mark + 60 s", [r for r in rows if r["lag"]], "a1")
    line("  whole set at the mark + 60 s", rows, "a1")
