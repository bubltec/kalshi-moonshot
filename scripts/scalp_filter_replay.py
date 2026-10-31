"""Which entries to take: the early scalp replayed on every crypto 15-minute window, with what each entry looked like, then results split by each feature. Read-only.

Entry, exit and fees are as scripts/scalp_range_replay.py (first second in the band between ENTRY_FROM and ENTRY_TO minutes, ask not lower than 10 s ago; follow rule: once the bid is GAIN over the price paid follow its high and sell 1 s
after a 4c dip; else held to the result). Features read at the entry second, for the side bought:
    fell60    the ask 60 s ago minus the ask now (positive: the price fell into the band; negative: it climbed into it)
    fell20    the same over 20 s
    spread    ask minus bid
    minute    minutes into the window
    flow_for  dollars takers paid FOR this side in the last 10 s, flow_1s (contracts x price paid), and flow_share = for / (for + against)
    coin
Each feature is cut into thirds (or by value), each cut reports bets, share reaching the gain, per bet, and the older / newer half. A cut of a few dozen bets is noise: a standard error is shown.

    scripts/cloud.sh run scripts/scalp_filter_replay.py [DAYS=7] [LO=0.55] [HI=0.60] [GAIN=0.10] [FEE=1]
    docker compose ... exec -T -e DAYS=3 bot python - < scripts/scalp_filter_replay.py
"""
import bisect
import math
import os
import re
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "7"))
LO, HI, GAIN = float(os.environ.get("LO", "0.55")), float(os.environ.get("HI", "0.60")), float(os.environ.get("GAIN", "0.10"))
FEE = os.environ.get("FEE", "1") == "1"
LAT, DIP = 1, 0.04
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
ENTRY_FROM, ENTRY_TO = 1.0, 9.0


def fee(p):
    return fee_per_contract(p) if FEE else 0.0


wins = main.db.execute("SELECT ticker, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (time.time() - DAYS * 86400,)).fetchall()
rows = []
for w in wins:
    m = re.match(r"^KX([A-Z]+)15M-", w["ticker"])
    if not m or m.group(1) not in COINS:
        continue
    close = float(w["close_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 960, close)).fetchall()
    if len(q) < 30:
        continue
    ts = [float(r[0]) for r in q]
    per = {}
    for sec in range(int(close - 960), int(close) + 1):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts[i] <= 15:
            per[sec] = (float(q[i][1]), float(q[i][2]))
    flow = {}
    for t, yv, nv, vw in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], int(close - 960), int(close))).fetchall():
        d = flow.setdefault(int(t), [0.0, 0.0])
        vw = 0.5 if vw is None else float(vw)
        d[0] += float(yv or 0) * vw
        d[1] += float(nv or 0) * (1.0 - vw)

    def held(sec, side):
        v = per.get(sec)
        if v is None:
            return None
        return (v[0], v[1]) if side == "yes" else (1.0 - v[1], 1.0 - v[0])  # (held bid, held ask)

    pick = None
    for sec in range(int(close - 900 + ENTRY_FROM * 60), int(close - 900 + ENTRY_TO * 60)):
        for side in ("yes", "no"):
            now, before = held(sec, side), held(sec - 10, side)
            if now is None or before is None or not (LO <= now[1] <= HI and now[1] >= before[1] - 1e-9):
                continue
            pick = (sec, side, now)
            break
        if pick:
            break
    if not pick:
        continue
    sec0, side, now = pick
    ask = now[1]
    cost = ask + fee(ask)
    arm, high, pnl, hit = ask + GAIN, 0.0, None, False
    for sec in range(sec0 + 1, int(close) - 5):
        h = held(sec, side)
        if h is None:
            continue
        if h[0] >= arm - 1e-9:
            high = max(high, h[0])
        if high and h[0] <= high - DIP + 1e-9:
            f = held(sec + LAT, side) or h
            pnl, hit = f[0] - fee(f[0]) - cost, True
            break
    if pnl is None:
        pnl = (1.0 if w["result"] == side else 0.0) - cost
    h60, h20 = held(sec0 - 60, side), held(sec0 - 20, side)
    idx_for = 0 if side == "yes" else 1
    f_for = sum(flow.get(sec0 - k, [0.0, 0.0])[idx_for] for k in range(10))
    f_ag = sum(flow.get(sec0 - k, [0.0, 0.0])[1 - idx_for] for k in range(10))
    rows.append({"pnl": pnl, "hit": hit, "close": close, "coin": m.group(1), "side": side, "ask": ask, "spread": now[1] - now[0],
                 "minute": (sec0 - (close - 900)) / 60.0, "fell60": (h60[1] - ask) if h60 else None, "fell20": (h20[1] - ask) if h20 else None,
                 "flow_for": f_for, "flow_share": f_for / (f_for + f_ag) if f_for + f_ag > 0 else None})
print(f"{len(rows)} entries in {LO:.2f}-{HI:.2f}, follow {GAIN:.2f}, fee {'on' if FEE else 'off'}, {DAYS:g} days\n")


def stats(rs):
    p = [r["pnl"] for r in rs]
    n = len(p)
    if n == 0:
        return "   0"
    m = sum(p) / n
    se = (sum((x - m) ** 2 for x in p) / n) ** 0.5 / math.sqrt(n) if n > 1 else float("nan")
    rs2 = sorted(rs, key=lambda r: r["close"])
    h = n // 2
    a = sum(r["pnl"] for r in rs2[:h]) / max(h, 1)
    b = sum(r["pnl"] for r in rs2[h:]) / max(n - h, 1)
    return f"{n:>4}  hit {100 * sum(1 for r in rs if r['hit']) / n:>3.0f}%  {m * 100:>+6.2f}c +-{se * 100:.2f}  old {a * 100:>+6.2f}c new {b * 100:>+6.2f}c"


print(f"{'all':<26} {stats(rows)}")
for key in ("fell60", "fell20", "spread", "minute", "flow_for", "flow_share"):
    have = sorted([r for r in rows if r[key] is not None], key=lambda r: r[key])
    if len(have) < 9:
        continue
    cuts = [have[: len(have) // 3], have[len(have) // 3: 2 * len(have) // 3], have[2 * len(have) // 3:]]
    print(f"\n{key}")
    for name, c in zip(("low third", "middle", "high third"), cuts):
        print(f"  {name:<10} {c[0][key]:>8.3f}..{c[-1][key]:<8.3f} {stats(c)}")
for key in ("coin", "side"):
    print(f"\n{key}")
    for v in sorted({r[key] for r in rows}):
        print(f"  {v:<22} {stats([r for r in rows if r[key] == v])}")
print("DONE")
