"""WHAT SEPARATES A FLIP FROM A SPIKE: when a held favorite's bid falls fast in the last two minutes, sometimes the side then LOSES (a flip: selling saved the bet) and sometimes it WINS anyway (a spike: selling threw it away). The price fall alone cannot tell them apart
(scripts/drop_exit_study.py: a 25c fall in 3 s sold better than holding on average, but the side still won 37% of the time). This builds one row per fall, with what was visible AT that second (taker dollars against and with the holder, the multiple of the market's own normal, how thin the held
side's book is, how fast and how far, time left, activity), and the outcome, then shows which features separate the two and tests rules on days the rule was not tuned on.

A fall: the held side's bid is `LOOSE` under its highest bid of the last 5 s, in the last 120 s to 10 s before the close, the side having been 0.76+ in the minute before. One row per window and side (the first such second). Sale = the bid one second later less the fee; hold = 1 if the side
wins else 0; `better` = sale - hold per contract (positive: selling was right). All features use data up to that second only; `bounce` (the best bid in the 10 s after) is for labelling and never a feature.
    SECTION=1 .. each feature in buckets: how often the side still won and how much better selling was
    SECTION=2 .. rules built from the features, tuned on the earlier 60% of windows and scored on the later 40%
    SECTION=3 .. wait k seconds after the fall and sell only if the bid is still `stay` under its high (a spike bounces back, a flip does not)
Read-only. Places nothing.    scripts/cloud.sh run scripts/drop_flip_vs_spike_study.py SECTION=1
"""
import bisect
import math
import os
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

SECTION = os.environ.get("SECTION", "1")
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
COMMODITY = ("GOLD", "SILVER", "WTI", "BRENT", "NATGAS", "COPPER", "PLATINUM", "PALLADIUM")
WINDOWS = [w for w in main.db.execute("SELECT ticker, series, result, close_ts FROM windows WHERE series LIKE '%15M' AND result IN ('yes','no') ORDER BY close_ts").fetchall() if w["series"][2:-3] in COMMODITY]
LOOSE, WAS = 0.15, 0.76
rows = []
for m in WINDOWS:
    close = float(m["close_ts"])
    q = tape.execute("SELECT ts, bid, ask, bid_size, ask_size FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"], close - 330, close)).fetchall()
    fl = tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts>? AND ts<=?", (m["ticker"], int(close - 335), int(close))).fetchall()
    if len(q) < 60 or len(fl) < 20:
        continue
    yd, nd = defaultdict(float), defaultdict(float)
    for ts, yv, nv, vw in fl:
        if vw is not None:
            yd[int(ts)] += float(yv) * float(vw)
            nd[int(ts)] += float(nv) * (1.0 - float(vw))
    ts_list = [float(r[0]) for r in q]
    bids, sizes = {}, {}
    for sec in range(int(close) - 330, int(close) + 1):
        i = bisect.bisect_right(ts_list, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts_list[i] <= 15:
            bids[sec] = {"yes": float(q[i][1]), "no": 1.0 - float(q[i][2])}
            sizes[sec] = (float(q[i][3] or 0.0), float(q[i][4] or 0.0), float(q[i][2]) - float(q[i][1]))
    for side, against, withh in (("yes", nd, yd), ("no", yd, nd)):
        for sec in range(int(close) - 120, int(close) - 10):
            now, after = bids.get(sec), bids.get(sec + 1)
            if not now or not after or sec not in sizes:
                continue
            if max((bids[x][side] for x in range(sec - 60, sec) if x in bids), default=0.0) < WAS:
                continue
            high5 = max((bids[x][side] for x in range(sec - 5, sec + 1) if x in bids), default=0.0)
            if high5 - now[side] < LOOSE - 1e-9:
                continue
            high3 = max((bids[x][side] for x in range(sec - 3, sec + 1) if x in bids), default=0.0)
            a3 = sum(against[x] for x in range(sec - 2, sec + 1))
            w3 = sum(withh[x] for x in range(sec - 2, sec + 1))
            a10 = sum(against[x] for x in range(sec - 9, sec + 1))
            w10 = sum(withh[x] for x in range(sec - 9, sec + 1))
            per = sorted(against[x] + withh[x] for x in range(sec - 122, sec - 2))
            base = max(3.0 * (per[59] + per[60]) / 2.0, 1.0)
            yb, ya, spread = sizes[sec]
            hb, ha = (yb, ya) if side == "yes" else (ya, yb)  # the held side's bid depth and ask depth, in contracts
            bounce = max((bids[x][side] for x in range(sec + 1, sec + 11) if x in bids), default=now[side]) - now[side]
            won = 1.0 if m["result"] == side else 0.0
            sale = max(after[side], 0.0) - (fee(after[side]) if after[side] > 0 else 0.0)
            prior = [bids[x][side] for x in range(sec - 60, sec - 5) if x in bids]
            rows.append({
                "close": close, "won": won, "better": sale - won, "sale": after[side], "bounce": bounce,
                "drop5": high5 - now[side], "drop3": high3 - now[side], "bid": now[side], "left": close - sec,
                "against3": a3, "share3": a3 / (a3 + w3 + 1e-9), "ratio": a3 / base, "share10": a10 / (a10 + w10 + 1e-9),
                "held_bid_depth": hb, "held_ask_depth": ha, "spread": spread, "activity": base / 3.0,
                "slid_before": (max(prior) - prior[-1]) if prior else 0.0,
                "high5": high5, "path": [bids[x][side] if x in bids else None for x in range(sec, sec + 14)],
            })
            break  # one row per window and side


def sem(xs):
    return statistics.pstdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else float("nan")


def line(label, sel):
    if not sel:
        return f"  {label:<28} -"
    b = [r["better"] for r in sel]
    return f"  {label:<28} n={len(sel):<4} side still won {100 * statistics.mean(r['won'] for r in sel):3.0f}%  sold at {statistics.mean(r['sale'] for r in sel):.2f}  selling better by {statistics.mean(b):+.3f} +-{sem(b):.3f}"


print(f"{len(WINDOWS)} commodity windows; {len(rows)} falls of {LOOSE:.2f}+ within 5 s (one per window and side)\n")
if SECTION == "1":
    print(line("all falls", rows), "\n")
    FEATURES = ("drop3", "drop5", "bid", "left", "against3", "share3", "ratio", "share10", "held_bid_depth", "held_ask_depth", "spread", "activity", "slid_before", "bounce")
    for f in FEATURES[:-1]:
        vals = sorted(r[f] for r in rows)
        cuts = sorted({vals[int(len(vals) * k / 4)] for k in (1, 2, 3)})
        edges = [-1e18, *cuts, 1e18]
        print(f"{f} (quartile edges {', '.join(f'{c:.2f}' for c in cuts)})")
        for lo, hi in zip(edges[:-1], edges[1:], strict=True):
            print(line(f"{'<' if lo < -1e17 else f'{lo:.2f}'} .. {'' if hi > 1e17 else f'{hi:.2f}'}", [r for r in rows if lo <= r[f] < hi]))
    print("\nspike or flip in hindsight (bounce = the best bid in the next 10 s above the bid at the fall; NOT usable live):")
    for lo, hi in ((-1, 0.02), (0.02, 0.10), (0.10, 0.20), (0.20, 2)):
        print(line(f"bounce {lo:.2f}..{hi:.2f}", [r for r in rows if lo <= r["bounce"] < hi]))
elif SECTION == "3":
    rows.sort(key=lambda r: r["close"])
    cut = int(len(rows) * 0.6)
    print("fall found at second 0; wait k seconds; sell at the bid one second after that check only if the bid is still >= stay under the 5 s high. Falls that bounced back are kept (not sold).\n")
    for stay in (0.10, 0.15, 0.20):
        for k in (0, 2, 4, 6, 8):
            def run(rs):
                out, kept = [], 0
                for r in rs:
                    p = r["path"]
                    if p[k] is None or p[k + 1] is None:
                        continue
                    if r["high5"] - p[k] >= stay - 1e-9:
                        sale = max(p[k + 1], 0.0) - (fee(p[k + 1]) if p[k + 1] > 0 else 0.0)
                        out.append({"won": r["won"], "sale": p[k + 1], "better": sale - r["won"]})
                    else:
                        kept += 1
                return out, kept
            a, ka = run(rows[:cut])
            b, kb = run(rows[cut:])
            print(f"stay>={stay:.2f} wait {k}s | " + line("train", a)[2:].strip() + f" (kept {ka}) | " + line("test", b)[2:].strip() + f" (kept {kb})")
else:
    rows.sort(key=lambda r: r["close"])
    cut = int(len(rows) * 0.6)
    train, test = rows[:cut], rows[cut:]
    print(f"tuned on {len(train)} falls (earlier), scored on {len(test)} (later)\n")
    rules = []
    for d3 in (0.0, 0.15, 0.20, 0.25):
        for sh in (0.0, 0.5, 0.75):
            for ra in (0.0, 3.0, 6.0, 10.0):
                for dp in (1e9, 30.0, 15.0):
                    rules.append((d3, sh, ra, dp))

    def pick(rs, rule):
        d3, sh, ra, dp = rule
        return [r for r in rs if r["drop3"] >= d3 and r["share3"] >= sh and r["ratio"] >= ra and r["held_bid_depth"] <= dp]

    scored = []
    for rule in rules:
        a = pick(train, rule)
        if len(a) >= 25:
            scored.append((statistics.mean(r["better"] for r in a), rule, a))
    scored.sort(key=lambda x: -x[0])
    print("rule = drop in 3 s >= d, share of 3 s dollars against >= s, multiple of normal >= x, held bid depth <= contracts (1e9 = any)")
    print("best on the earlier 60% (n >= 25), and the same rule on the later 40%:")
    for _, rule, a in scored[:8]:
        print(f"  d>={rule[0]:.2f} s>={rule[1]:.2f} x>={rule[2]:>4.1f} depth<={rule[3]:<6.0f}")
        print("   train" + line("", a)[2:])
        print("   test " + line("", pick(test, rule))[2:])
    print("\nthe plain price rule on the same split (what is live):")
    live = (0.25, 0.0, 0.0, 1e9)
    print("   train" + line("", pick(train, live))[2:])
    print("   test " + line("", pick(test, live))[2:])
