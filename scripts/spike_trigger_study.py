"""WHICH TRIGGER CATCHES A CRASH-OUT THAT FLIPS: the moonshot buys the cheap side in the last 120 s when buying against the favorite spikes. This replays candidate triggers over EVERY finished crypto 15-minute window on the tape, not only the windows the
live trigger fired in, so a rule is judged on all the moments it would have fired.

Each second from 120 s to 10 s before the close, for each side priced 2-20c (the lane's band): the dollars the takers of THAT side bought in the last 3 s (YES takers pay the YES price, NO takers 1 - the YES price; the tape keeps one VWAP a second, so these are
close to, not exactly, the bot's own figure), and the multiple of that side's own normal (median second of the 2 minutes before). The lane's guards apply as they do live: the ask parked for 20 s and not fallen 10c in 12 s. A rule fires at the first second a side
passes it, once per window. Buying that side at its ask (taker fee paid) wins when the side wins at settlement.

Reported per rule: windows fired, how often the cheap side won (the flip), its average price, and the return per dollar staked held to the result, with a standard error. A fair price returns about minus the fee, so a rule has to beat the average price in win rate
before it is worth anything. The "any second" row is that control: the first second the guards pass with no volume condition.
Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/spike_trigger_study.py
"""
import bisect
import datetime as dt
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
LO, HI = 0.02, 0.20  # the lane's price band
CRASH, CRASH_SEC, PARK_SEC, PARK_HIGH, PARK_SPAN = 0.10, 12.0, 20.0, 0.10, 0.06  # the lane's guard settings


def parked_and_calm(qts, qask, now, ask):
    """The lane's guards on this side's ask series, as the bot sees it at time `now`: on screen for PARK_SEC and never above the cap, within PARK_SPAN of the ask, and not fallen CRASH in CRASH_SEC."""
    hi12 = 0.0
    oldest = -1.0
    high20 = 0.0
    for ts, a in zip(qts, qask, strict=True):
        age = now - ts
        if age < 0 or age > PARK_SEC:
            continue
        oldest = max(oldest, age)
        high20 = max(high20, a)
        if age <= CRASH_SEC:
            hi12 = max(hi12, a)
    if oldest < PARK_SEC - 5.0 or high20 <= 0:
        return False
    if ask <= hi12 - CRASH:
        return False
    return high20 <= max(HI, PARK_HIGH) and high20 <= ask + PARK_SPAN


def windows():
    for m in main.db.execute(f"SELECT ticker, series, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(SERIES))}) AND result IN ('yes','no') ORDER BY close_ts", SERIES).fetchall():
        close = float(m["close_ts"])
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"], close - 270, close)).fetchall()
        fl = tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (m["ticker"], int(close - 275), int(close))).fetchall()
        if len(q) < 60 or len(fl) < 30:
            continue
        yield m["ticker"], m["series"], m["result"], close, q, fl


def candidates(q, fl, close):
    """Every (second, side, ask, dollars in 3 s, multiple of normal, favorite price) in the last 120 s where the side is in the band and the guards pass."""
    yd = defaultdict(float)
    nd = defaultdict(float)
    for ts, yv, nv, vw in fl:
        if vw is None:
            continue
        yd[int(ts)] += float(yv) * float(vw)
        nd[int(ts)] += float(nv) * (1.0 - float(vw))
    qts = [float(r[0]) for r in q]
    out = []
    for t in range(int(close) - 120, int(close) - 9):
        i = bisect.bisect_right(qts, t + 0.999) - 1
        if i < 0 or t + 0.999 - qts[i] > 10:
            continue
        bid, ask = float(q[i][1]), float(q[i][2])
        for side, d, px in (("yes", yd, ask), ("no", nd, 1.0 - bid)):
            if not (LO <= px <= HI):
                continue
            qask = [float(r[2]) if side == "yes" else 1.0 - float(r[1]) for r in q[max(0, i - 40): i + 1]]
            if not parked_and_calm(qts[max(0, i - 40): i + 1], qask, t + 0.999, px):
                continue
            d3 = d[t] + d[t - 1] + d[t - 2]
            prior = sorted(d[t - 3 - k] for k in range(120))
            base = (prior[59] + prior[60]) / 2.0
            ratio = d3 / (3.0 * base + 0.01)
            cheap_mid = (bid + ask) / 2.0 if side == "yes" else 1.0 - (bid + ask) / 2.0
            out.append((t, side, px, d3, ratio, 1.0 - cheap_mid))
    return out


RULES = {}


def add(name, fn):
    RULES[name] = fn


add("any second the guards pass (control)", lambda c, btc: True)
add("CURRENT: $ bar (BTC 800, others 400)", lambda c, btc: c[3] >= (800 if btc else 400))
for B in (200, 400, 1600, 3200):
    add(f"$ bar {B} (all coins)", lambda c, btc, B=B: c[3] >= B)
for K in (2, 3, 5, 8, 12):
    add(f"{K}x normal, and at least $100", lambda c, btc, K=K: c[4] >= K and c[3] >= 100)
for K in (3, 5):
    add(f"CURRENT bar and {K}x normal", lambda c, btc, K=K: c[3] >= (800 if btc else 400) and c[4] >= K)
add("CURRENT bar, favorite 90c+", lambda c, btc: c[3] >= (800 if btc else 400) and c[5] >= 0.90)
add("5x normal and $100, favorite 90c+", lambda c, btc: c[4] >= 5 and c[3] >= 100 and c[5] >= 0.90)

results = {n: [] for n in RULES}
n_windows = 0
for tk, series, result, close, q, fl in windows():
    n_windows += 1
    cs = candidates(q, fl, close)
    btc = series == "KXBTC15M"
    for name, fn in RULES.items():
        for c in cs:
            if fn(c, btc):
                won = 1.0 if result == c[1] else 0.0
                results[name].append((tk, series, c[0], c[2], won, c[3], c[4]))
                break


def summarize(rows):
    if not rows:
        return None
    n = len(rows)
    wins = sum(r[4] for r in rows)
    px = statistics.mean(r[3] for r in rows)
    rets = [(r[4] - r[3] - fee(r[3])) / (r[3] + fee(r[3])) for r in rows]
    se = (statistics.pstdev(rets) / math.sqrt(n)) if n > 1 else float("nan")
    p = wins / n
    return n, wins, p, math.sqrt(p * (1 - p) / n), px, statistics.mean(rets), se


print(f"{n_windows} finished windows replayed (five coins), lane band {LO:.2f}-{HI:.2f}\n")
print(f"{'rule':44} {'fires':>5} {'won':>4} {'win %':>6} {'+-':>5} {'avg ask':>8} {'return/$ (fee paid)':>20} {'+-':>6}")
for name in RULES:
    r = summarize(results[name])
    if r is None:
        print(f"{name:44} {0:>5}")
        continue
    n, w, p, sp, px, ret, se = r
    print(f"{name:44} {n:>5} {int(w):>4} {100*p:>5.1f}% {100*sp:>4.1f}% {px:>8.3f} {ret:>+19.1%} {100*se:>5.1f}%")

print("\nBTC only / not BTC (CURRENT vs 5x normal and $100 vs control):")
for name in ("any second the guards pass (control)", "CURRENT: $ bar (BTC 800, others 400)", "5x normal, and at least $100", "8x normal, and at least $100"):
    for label, pick in (("BTC", lambda r: r[1] == "KXBTC15M"), ("not BTC", lambda r: r[1] != "KXBTC15M")):
        r = summarize([x for x in results[name] if pick(x)])
        if r:
            n, w, p, sp, px, ret, se = r
            print(f"  {name:40} {label:8} fires {n:>4} won {int(w):>3} ({100*p:.1f}%) avg ask {px:.3f} return {ret:+.1%} +-{100*se:.1f}%")

cur = results["CURRENT: $ bar (BTC 800, others 400)"]
print("\nCross-check against the live bot: the CURRENT rule's replayed firings since 10-27 18:46 (the bot's real BTC bets were at 18:59:06, 19:28:02, 19:58:18, 20:28:05, 20:43:09):")
since = dt.datetime(2026, 10, 27, 18, 46).timestamp()
for r in cur:
    if r[2] >= since:
        print(f"  replay fires {r[0]} at {dt.datetime.fromtimestamp(r[2]).strftime('%H:%M:%S')} ask {r[3]:.3f} dollars {r[5]:.0f} ({r[6]:.1f}x normal) -> cheap side {'WON' if r[4] else 'lost'}")
