"""Quiet after a step: do commodity bets bought when the curve has compressed and the spot sits clear of the target pay more than their price? (2026-11-19, user, on KXBRENT15M-26NOV191730, YES bought 0.69, held, settled YES: "this was a great win ... more like what i've been looking for from the commodity lanes"; "compared to the hour it was thin, but i think around 5pm this happens".)

One question on the same bets (CLAUDE.md rules 1 and 4): every live commodity bet (mode trade, action bet, a lane starting commodity, not manual) with a settled result since SINCE (default 2026-11-11 00:00 ET, where the Kalshi perp tape on the host begins) and an hour of the series' own Kalshi perp reference ticks (index KP_<series>). The time of day is NOT a reading; the curve is. Readings at the buy, in the market's own units:
  comp      the typical minute of the last 10 minutes over the typical minute of the hour before them (typical minute = rms one-second move times sqrt(60)); under 1 means the curve has gone quiet. At 5 pm on Brent the clock-free version of "it flattens".
  cushion   how far the spot sits clear of the bet's centre (the number the bot's own note carries: "center X"), on the bet's side, in the last 10 minutes' typical minutes scaled to the time left (unit10 x sqrt(left/60)): the move that would be needed to end up on the other side.
  held      share of the last 5 minutes the spot spent on the bet's side of the centre.
Cuts are fixed before the run (a study's cuts, said so): comp under 0.5, 0.5 to 1, over 1; cushion under 0.5, 0.5 to 1, 1 to 2, over 2; held under 1, exactly 1. Per cut: bets, average ask, win rate against it, net per $100 staked held to settlement with the buy's fee and as traded (own exit, fee on the sale), bets ended under 0.10, each half of the period. The shapes are crossed (comp x cushion) because the user's point is that it is the combination. The example bet is printed for reference. Settings chosen after seeing the results are said to be so.
Run on the host: scripts/cloud.sh run scripts/commodity_compression.py [SINCE=<epoch>]"""
import datetime
import math
import os
import re

from kalshi_moonshot import config, db

ET = datetime.timezone(datetime.timedelta(hours=-5))
since = float(os.environ.get("SINCE", datetime.datetime(2026, 11, 11, tzinfo=ET).timestamp()))
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)


def fee(p):
    return 0.07 * p * (1.0 - p)


rows = st.execute("SELECT d.ts, d.ticker, d.side, d.ask, d.exit_price, d.context, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.lane LIKE 'commodity%' AND d.lane<>'manual' AND d.ts>? AND w.result IN ('yes','no') ORDER BY d.ts", (since,)).fetchall()
bets, nodata = [], 0
for b in rows:
    if not b["ask"] or not b["close_ts"]:
        continue
    m = re.search(r"center ([0-9.]+)", b["context"] or "")
    if not m:
        nodata += 1
        continue
    center = float(m.group(1))
    t0 = float(b["ts"])
    left = float(b["close_ts"]) - t0
    ix = "KP_" + b["ticker"].split("-")[0].removeprefix("KX").removesuffix("15M")
    q = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>=? AND ts<=? AND value>0 ORDER BY ts", (ix, t0 - 3600.0, t0)).fetchall()
    if len(q) < 1200 or left <= 0:
        nodata += 1
        continue
    ts = [float(x[0]) for x in q]
    v = [float(x[1]) for x in q]
    grid, j = [], 0
    for sec in range(int(t0) - 3600, int(t0)):
        while j + 1 < len(ts) and ts[j + 1] <= sec:
            j += 1
        grid.append(v[j] if ts[j] <= sec else None)
    if any(x is None for x in grid[-600:]) or sum(1 for x in grid if x is None) > 600:
        nodata += 1
        continue
    g = [x for x in grid if x is not None]

    def unit_of(xs):
        dd = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        return math.sqrt(sum(x * x for x in dd) / max(len(dd), 1)) * math.sqrt(60.0)

    u10, uprev = unit_of(g[-600:]), unit_of(g[:-600])
    if u10 <= 0 or uprev <= 0:
        nodata += 1
        continue
    spot = g[-1]
    sign = 1.0 if b["side"] == "yes" else -1.0
    cushion = sign * (spot - center) / (u10 * math.sqrt(left / 60.0))
    last5 = g[-300:]
    held_share = sum(1 for x in last5 if sign * (x - center) > 0) / len(last5)
    ask = float(b["ask"])
    won = b["result"] == b["side"]
    sold = b["exit_price"] is not None
    heldnet = (1.0 if won else 0.0) - ask - fee(ask)
    traded = ((float(b["exit_price"]) - fee(float(b["exit_price"]))) if sold else (1.0 if won else 0.0)) - ask - fee(ask)
    end = float(b["exit_price"]) if sold else (1.0 if won else 0.0)
    bets.append({"t": t0, "ticker": b["ticker"], "ask": ask, "won": won, "held": heldnet, "traded": traded, "zero": end < 0.10, "comp": u10 / uprev, "cushion": cushion, "heldshare": held_share})
bets.sort(key=lambda r: r["t"])
n = len(bets)
first = set(id(r) for r in bets[: n // 2])
print(f"window: {datetime.datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET to now; {n} live commodity bets with an hour of Kalshi perp ticks and the bet's centre ({nodata} left out). Cuts fixed before the run.")


def show(label, sel):
    if len(sel) < 15:
        print(f"  {label:44s} bets {len(sel):4d}")
        return
    stake = sum(r["ask"] for r in sel)
    f = lambda xs, k: 100.0 * sum(r[k] for r in xs) / max(sum(r["ask"] for r in xs), 1e-9) if xs else float("nan")
    h1 = [r for r in sel if id(r) in first]
    h2 = [r for r in sel if id(r) not in first]
    print(f"  {label:44s} bets {len(sel):4d} | avg ask {100.0 * stake / len(sel):5.1f}c win {100.0 * sum(1 for r in sel if r['won']) / len(sel):5.1f}% | held {sum(r['held'] for r in sel):+7.2f} ({f(sel, 'held'):+5.1f}/$100) | traded {sum(r['traded'] for r in sel):+7.2f} ({f(sel, 'traded'):+5.1f}/$100) | ended under 0.10: {sum(1 for r in sel if r['zero'])} | held halves {f(h1, 'held'):+5.1f} / {f(h2, 'held'):+5.1f}")


show("all", bets)
print("--- comp (last 10 min typical minute over the hour before; under 1 = gone quiet)")
CO = (("under 0.5", lambda r: r["comp"] < 0.5), ("0.5 to 1", lambda r: 0.5 <= r["comp"] < 1.0), ("1 or more", lambda r: r["comp"] >= 1.0))
CU = (("under 0.5", lambda r: r["cushion"] < 0.5), ("0.5 to 1", lambda r: 0.5 <= r["cushion"] < 1.0), ("1 to 2", lambda r: 1.0 <= r["cushion"] < 2.0), ("2 or more", lambda r: r["cushion"] >= 2.0))
for name, f_ in CO:
    show(f"comp {name}", [r for r in bets if f_(r)])
print("--- cushion (spot clear of the centre on the bet's side, in unit10 x sqrt(left/60); negative = on the wrong side)")
for name, f_ in CU:
    show(f"cushion {name}", [r for r in bets if f_(r)])
print("--- held share of the last 5 minutes on the bet's side")
show("held under 1", [r for r in bets if r["heldshare"] < 1.0])
show("held exactly 1", [r for r in bets if r["heldshare"] >= 1.0])
print("--- comp x cushion")
for cn, cf in CO:
    for un, uf in CU:
        show(f"comp {cn} / cushion {un}", [r for r in bets if cf(r) and uf(r)])
print("--- the example")
for r in bets:
    if r["ticker"].startswith("KXBRENT15M-26NOV191730"):
        print(f"  KXBRENT15M-26NOV191730: ask {r['ask']:.2f}, comp {r['comp']:.2f}, cushion {r['cushion']:.2f}, held share {r['heldshare']:.2f}, won {r['won']}")
print("DONE")
