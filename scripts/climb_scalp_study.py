"""A side that climbs from 20c to 40c inside a minute: buy it at 40c, rest a sell at 50c, with a stop that locks the gain and a timed exit.

Every settled crypto 15-minute market in the recorded tape is replayed, once per side. A side is armed when its ask prints ARM or less. It
is bought when its ask first reaches BUY, provided that is within WINDOW seconds of the last print at ARM or less (that is the climb;
a slower rise is dropped) and at least MIN_LEFT seconds remain. The buy is a taker buy at the ask, and is skipped when the ask has
already gapped past BUY + SLIP (a 40c limit would not fill). After the fill, in the order of each quote:
  - the sell at TARGET fills at TARGET the moment the side's bid touches it;
  - with EXIT_LEFT seconds left, whatever is still held is sold at the bid (0 = no timed exit);
  - the lock: once the bid has been UPARM over the price paid, a fall back to LOCK over the price paid sells at the bid
    (LOCK 0 = breakeven; UPARM 0 = no lock). A bid that gaps under LOCK sells where it landed.
Anything not sold settles. One entry per market per side. Returns are per dollar staked, before fees. The tape keeps about one quote per
second, so a touch between quotes is not seen.

Read-only. Places nothing.
    scripts/cloud.sh run scripts/climb_scalp_study.py [DAYS=4] [ARM=0.20] [BUY=0.40] [TARGET=0.50] [WINDOW=60] [SLIP=0.03] [MIN_LEFT=20] [EXIT_LEFT=120] [SIDES=yes,no]
"""
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.journal import cluster_se
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "4"))
ARM = float(os.environ.get("ARM", "0.20"))
BUY = float(os.environ.get("BUY", "0.40"))
TARGET = float(os.environ.get("TARGET", "0.50"))
WINDOW = float(os.environ.get("WINDOW", "60"))
SLIP = float(os.environ.get("SLIP", "0.03"))
MIN_LEFT = float(os.environ.get("MIN_LEFT", "20"))
EXIT_LEFT = float(os.environ.get("EXIT_LEFT", "120"))
SIDES = tuple(os.environ.get("SIDES", "yes,no").split(","))
COINS = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
# (name, exit_left, uparm, lock)
VARIANTS = (
    ("sell 50c only", 0.0, 0.0, 0.0),
    ("+ exit at 120s left", EXIT_LEFT, 0.0, 0.0),
    ("+ lock: up 3c, stop at cost", EXIT_LEFT, 0.03, 0.0),
    ("+ lock: up 5c, stop at cost", EXIT_LEFT, 0.05, 0.0),
    ("+ lock: up 5c, stop at +2c", EXIT_LEFT, 0.05, 0.02),
    ("+ lock: up 8c, stop at +3c", EXIT_LEFT, 0.08, 0.03),
)


def entry_of(quotes, side, close_ts):
    """(index, price) of the buy, 'gapped' when the climb was there but the ask ran past the limit, or None."""
    armed = None
    for i, (ts, bid, ask) in enumerate(quotes):
        a = ask if side == "yes" else 1.0 - bid
        if a <= ARM:
            armed = ts
            continue
        if armed is None or a < BUY:
            continue
        if ts - armed > WINDOW:
            armed = None  # that climb took too long: dropped
            continue
        if close_ts - ts < MIN_LEFT:
            return None
        if a > BUY + SLIP + 1e-9:
            return "gapped"
        return i, a
    return None


def exit_of(quotes, side, i, entry, close_ts, won, exit_left, uparm, lock):
    """(state, roi) for a bought position: 'hit', 'timed', 'lock', 'settle_win' or 'settle_lose'."""
    up = False
    for ts, bid, ask in quotes[i + 1:]:
        b = bid if side == "yes" else 1.0 - ask
        if b >= TARGET - 1e-9:
            return "hit", TARGET / entry - 1.0
        if exit_left > 0 and close_ts - ts <= exit_left:
            return "timed", b / entry - 1.0
        if uparm > 0:
            if b >= entry + uparm - 1e-9:
                up = True
            if up and b <= entry + lock + 1e-9:
                return "lock", b / entry - 1.0
    return ("settle_win", 1.0 / entry - 1.0) if won else ("settle_lose", -1.0)


since = time.time() - DAYS * 86400
rows = main.db.execute(
    "SELECT ticker, series, close_ts - 900 AS open_ts, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? AND series IN (%s) ORDER BY close_ts"
    % ",".join("?" * len(COINS)),
    (since, *COINS),
).fetchall()

# results[side][variant] = list of (slot, state, roi)
results = {sd: [[] for _ in VARIANTS] for sd in SIDES}
signals = {sd: 0 for sd in SIDES}
gapped = {sd: 0 for sd in SIDES}
markets = 0
for r in rows:
    quotes = tape.execute(
        "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts",
        (r["ticker"], r["open_ts"] or 0, r["close_ts"]),
    ).fetchall()
    if len(quotes) < 20:
        continue
    markets += 1
    close_ts = float(r["close_ts"])
    for sd in SIDES:
        got = entry_of(quotes, sd, close_ts)
        if got is None:
            continue
        signals[sd] += 1
        if got == "gapped":
            gapped[sd] += 1
            continue
        i, entry = got
        for k, (_, exit_left, uparm, lock) in enumerate(VARIANTS):
            state, roi = exit_of(quotes, sd, i, entry, close_ts, r["result"] == sd, exit_left, uparm, lock)
            results[sd][k].append((int(close_ts), state, roi))

print(f"{markets} settled crypto 15M markets, last {DAYS:g} days. arm<={ARM:.2f}, buy {BUY:.2f} within {WINDOW:g}s (fill up to {BUY + SLIP:.2f}), sell {TARGET:.2f}")
for sd in SIDES:
    n = len(results[sd][0])
    print(f"\n{'UP (yes)' if sd == 'yes' else 'DOWN (no), same rule'}: {signals[sd]} signals, {gapped[sd]} gapped past the limit, {n} bought")
    print(f"{'exits':<30} {'hit':>4} {'lock':>5} {'timed':>6} {'settleW':>8} {'settleL':>8} {'mean ROI':>9} {'SE':>6} {'worst':>7}", flush=True)
    for (name, *_), res in zip(VARIANTS, results[sd], strict=True):
        if not res:
            continue
        mean = sum(x[2] for x in res) / len(res)
        se = cluster_se([(slot, roi) for slot, _, roi in res], mean) if len(res) > 1 else 0.0
        cnt = lambda k: sum(1 for x in res if x[1] == k)
        print(f"{name:<30} {cnt('hit'):>4} {cnt('lock'):>5} {cnt('timed'):>6} {cnt('settle_win'):>8} {cnt('settle_lose'):>8} {mean:>+9.1%} {se:>6.1%} {min(x[2] for x in res):>+7.0%}")
