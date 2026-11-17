"""Which curve shapes at the moment of the buy win more often than they cost? (2026-11-17, user: "Let's continue to work on our buys so we profit more. It has been making so little even at $10 bets";
"ok lets look at those shapes in that period".)

One question on the bot's own bets (CLAUDE.md rule 4): every live buy since the risk exit went live (2026-11-13 11:02 ET; SINCE/UNTIL=YYYY-MM-DDTHH:MM for another window), commodity lanes and
crypto_multiscale apart. The result of the BUY is the bet held to settlement, fee in (what was staked against what it paid), so no exit is in it; what actually happened (the lane's own sale or settlement)
is beside it. Shapes are read from Kalshi's own quote for the side bought, from the market's open to the buy, in the market's own typical minutes (variants.quote_wave_features: sqrt(60) times the rms
one-second move of the side's mid). The bins were fixed before the run and none was fitted:
  place    where the side's mid sits in its own range since the open (top fifth / lower)
  last 60  the side's move over the last 60 s (up half a typical minute or more / flat / down half or more)
  led      the share of the market so far in which the side was the favorite (variants.quote_led_share: under half / half to 0.9 / over 0.9)
  crosses  times the mid crossed even (0.50) since the open, on a 5 s grid (0 / 1 to 2 / 3 or more)
  range    how far the side's mid has travelled, high to low, since the open (under 3 typical minutes / 3 to 6 / over 6)
  path     path efficiency since the open on a 5 s grid: net move over the sum of moves (under 0.15 choppy / 0.15 to 0.4 / over 0.4 straight)
  when     minutes into the market at the buy (under 7 / 7 to 10 / over 10): the reading the lanes gate on, for reference
Then the same tables inside two price bands (paid 0.70 to 0.80 and 0.80 to 0.88). The price paid is the stake over the contracts filled, not the decision's ask. No coin or commodity is named in a split.
    scripts/cloud.sh run scripts/buy_shapes_study.py
"""
import bisect
import datetime
import os
import sys
import traceback

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.strategy import fee_per_contract as fee

sys.excepthook = lambda *e: print("".join(traceback.format_exception(*e)))
ET = datetime.timezone(datetime.timedelta(hours=-5))
when = lambda k, d: datetime.datetime.strptime(os.environ[k], "%Y-%m-%dT%H:%M").replace(tzinfo=ET).timestamp() if os.environ.get(k) else d
SINCE = when("SINCE", datetime.datetime(2026, 11, 13, 11, 2, tzinfo=ET).timestamp())
UNTIL = when("UNTIL", 9e18)
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
results = {w["ticker"]: (w["result"], float(w["close_ts"])) for w in st.execute("SELECT ticker, close_ts, result FROM windows WHERE close_ts>? AND result IN ('yes','no')", (SINCE,)).fetchall()}
rows = st.execute("SELECT ts, ticker, lane, side, ask, stake_usd, contracts, exit_price, exit_ts FROM decisions WHERE (lane LIKE 'commodity%' OR lane='crypto_multiscale') AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND ts<? ORDER BY ts", (SINCE, UNTIL)).fetchall()
bets, noread = [], 0
for r in rows:
    if r["ticker"] not in results:
        continue
    res, close = results[r["ticker"]]
    t0, side, opened = float(r["ts"]), r["side"], close - 900.0
    stake, n0, ask = float(r["stake_usd"]), float(r["contracts"] or 0.0), float(r["ask"] or 0.0)
    basis = stake / n0 if n0 > 0 else ask + fee(ask)  # what was paid a contract, fee in: stake over contracts agrees with the orders table on this window (5,901.13 and 8,337 contracts on crypto_multiscale), where the decision's ask runs 1 to 6c over the fill on most bets
    n = stake / basis
    won = res == side
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and float(r["exit_ts"]) < close - 1
    b = {"grp": "commodity" if r["lane"].startswith("commodity") else "crypto_multiscale", "stake": stake, "basis": basis, "won": won, "held": n * (1.0 if won else 0.0) - stake,
         "actual": ((float(r["exit_price"]) - fee(float(r["exit_price"])) if sold else (1.0 if won else 0.0)) - basis) * n, "into": (t0 - opened) / 60.0}
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], opened - 20, t0)).fetchall()
    mids = [(float(x[0]), (float(x[1]) + float(x[2])) / 2) for x in q]
    wave = variants.quote_wave_features(mids, side, opened, t0) if mids else None
    led = variants.quote_led_share(mids, side, opened, t0) if mids else None
    if wave is None or led is None:
        noread += 1
        b["read"] = False
        bets.append(b)
        continue
    qt = [m[0] for m in mids]

    def mid_at(t, mids=mids, qt=qt, side=side):
        i = bisect.bisect_right(qt, t) - 1
        i = max(i, 0)
        return mids[i][1] if side == "yes" else 1.0 - mids[i][1]

    start = max(opened, qt[0])
    sec = [mid_at(start + k) for k in range(int(t0 - start) + 1)]
    sq = sum((y - x) ** 2 for x, y in zip(sec, sec[1:]))
    unit = (sq / max(len(sec) - 1, 1)) ** 0.5 * 60**0.5
    g5 = sec[::5]
    steps = sum(abs(y - x) for x, y in zip(g5, g5[1:]))
    b |= {"read": True, "pos": wave["pos"], "amp": wave["amp"], "led": led, "m60": (sec[-1] - sec[max(len(sec) - 61, 0)]) / unit if unit > 0 else 0.0,
          "cross": sum(1 for x, y in zip(g5, g5[1:]) if (x - 0.5) * (y - 0.5) < 0), "eff": abs(g5[-1] - g5[0]) / steps if steps > 0 else 0.0}
    bets.append(b)


def line(name, rs):
    if not rs:
        print(f"    {name:34s} n   0")
        return
    stake = sum(x["stake"] for x in rs)
    held = sum(x["held"] for x in rs)
    print(f"    {name:34s} n {len(rs):3d} | staked ${stake:7.0f} | won {100 * sum(x['won'] for x in rs) / len(rs):3.0f}% at {sum(x['basis'] * x['stake'] for x in rs) / stake:.3f} paid | held to settlement ${held:+8.2f} ({100 * held / stake:+5.1f}% of stake) | as traded ${sum(x['actual'] for x in rs):+8.2f}")


SHAPES = [
    ("place in its own range since the open", [("top fifth", lambda x: x["pos"] >= 0.8), ("lower", lambda x: x["pos"] < 0.8)]),
    ("the last 60 s, in typical minutes", [("up half or more", lambda x: x["m60"] >= 0.5), ("flat", lambda x: -0.5 < x["m60"] < 0.5), ("down half or more", lambda x: x["m60"] <= -0.5)]),
    ("share of the market it has led", [("under half", lambda x: x["led"] < 0.5), ("half to 0.9", lambda x: 0.5 <= x["led"] < 0.9), ("over 0.9", lambda x: x["led"] >= 0.9)]),
    ("crossings of even since the open", [("none", lambda x: x["cross"] == 0), ("1 or 2", lambda x: 1 <= x["cross"] <= 2), ("3 or more", lambda x: x["cross"] >= 3)]),
    ("range since the open, typical minutes", [("under 3", lambda x: x["amp"] < 3), ("3 to 6", lambda x: 3 <= x["amp"] < 6), ("over 6", lambda x: x["amp"] >= 6)]),
    ("path since the open", [("choppy (under 0.15)", lambda x: x["eff"] < 0.15), ("0.15 to 0.4", lambda x: 0.15 <= x["eff"] < 0.4), ("straight (over 0.4)", lambda x: x["eff"] >= 0.4)]),
    ("minutes into the market", [("under 7", lambda x: x["into"] < 7), ("7 to 10", lambda x: 7 <= x["into"] < 10), ("over 10", lambda x: x["into"] >= 10)]),
]
print(f"window: live buys {datetime.datetime.fromtimestamp(SINCE, ET):%m-%d %H:%M} to {datetime.datetime.fromtimestamp(min(UNTIL, datetime.datetime.now().timestamp()), ET):%m-%d %H:%M} ET; {len(bets)} settled; {noread} without a curve reading (under 180 s of quotes before the buy, or the recorder was not writing)")
for grp in ("commodity", "crypto_multiscale"):
    g = [x for x in bets if x["grp"] == grp]
    print(f"\n=== {grp}, by the price paid a contract (fee in)")
    for name, lo, hi in (("under 0.58", 0, 0.58), ("0.58 to 0.70", 0.58, 0.70), ("0.70 to 0.80", 0.70, 0.80), ("0.80 to 0.88", 0.80, 0.88), ("0.88 and over", 0.88, 2)):
        line(name, [x for x in g if lo <= x["basis"] < hi])
    for label, pick in (("all prices", lambda x: True), ("paid 0.70 to 0.80", lambda x: 0.70 <= x["basis"] < 0.80), ("paid 0.80 to 0.88", lambda x: 0.80 <= x["basis"] < 0.88)):
        g = [x for x in bets if x["grp"] == grp and pick(x)]
        print(f"\n=== {grp}, {label}")
        line("all", g)
        line("no curve reading", [x for x in g if not x["read"]])
        rd = [x for x in g if x["read"]]
        for title, bins in SHAPES:
            print(f"  {title}")
            for name, f in bins:
                line(name, [x for x in rd if f(x)])
print("DONE")
