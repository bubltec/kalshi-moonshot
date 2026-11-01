"""Replay the strike-through sale over every live crypto_favorite bet and score variants of its three knobs against holding to the close. Places nothing.

The rule (watcher._strike_through_hit): the index going `line` percent of the strike through against the side starts a watch; a return to the winning side before it sells holds the bet to the close;
`stay` seconds continuously through sells at the bid then; in the last `window` seconds a turn back down after a bounce, or a pause of half the window, sells too. A bid of 0.99+ is left to settle.
Result per variant: bets sold, dollars (stake x return) against the same bets held, and how many sold bets would have won anyway.
    scripts/cloud.sh run scripts/strike_through_replay.py [SINCE=2026-10-29]
"""
import bisect
import itertools
import os
from collections import defaultdict
from datetime import datetime, timezone

from kalshi_moonshot import config, db
from kalshi_moonshot.digest import EXIT_SLIP
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

DUMP = os.environ.get("DUMP")  # a file written by DUMP_OUT=1 on the host: replay offline, with no database
if DUMP:
    import json
    data = [(r["bet"], r["ticks"], r["qts"], r["bids"]) for r in json.load(open(DUMP))]
    print(f"{len(data)} live crypto_favorite bets from {DUMP}")
else:
    s = config.reload()
    tape = db.connect(s.tape_path, "tape", shared=True)
    main = Store(s.db_path, s.exclude_versions)
    since = datetime.fromisoformat(os.environ.get("SINCE", "2026-10-29")).replace(tzinfo=timezone.utc).timestamp()

    bets = main.db.execute(
        "SELECT d.ts, d.ticker, d.side, d.ask, d.stake_usd, d.exit_price, d.exit_reason, d.outcome, w.strike, w.close_ts, w.series "
        "FROM decisions d JOIN windows w ON w.ticker = d.ticker WHERE d.lane = 'crypto_favorite' AND d.action = 'bet' AND d.mode = 'trade' AND d.ts > ? AND d.outcome IN ('yes','no') AND w.strike > 0 ORDER BY d.ts", (since,)).fetchall()
    print(f"{len(bets)} live crypto_favorite bets since {os.environ.get('SINCE', '2026-10-29')}", file=__import__("sys").stderr)

    def paths(b):
        ix = index_for_series(b["series"])
        ticks = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value > 0 ORDER BY ts", (ix, b["ts"], b["close_ts"])).fetchall()
        quotes = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid > 0 AND ask > 0 ORDER BY ts", (b["ticker"], b["ts"], b["close_ts"] + 5)).fetchall()
        return ticks, [(q[0], q[1] if b["side"] == "yes" else 1.0 - q[2]) for q in quotes]  # the NO bid is one minus the YES ask

    data = []
    for b in bets:
        ticks, quotes = paths(b)
        data.append((dict(zip(b.keys(), tuple(b))), [tuple(x) for x in ticks], [q[0] for q in quotes], [q[1] for q in quotes]))
    if os.environ.get("DUMP_OUT"):
        import json
        print("JSON>>" + json.dumps([{"bet": b, "ticks": t, "qts": q, "bids": d} for b, t, q, d in data]))
        raise SystemExit(0)
dens = [len(t) / max(b["close_ts"] - b["ts"], 1) for b, t, _, _ in data if t]
print(f"index ticks per second: median {sorted(dens)[len(dens)//2]:.2f}; bets with no ticks: {sum(1 for _, t, _, _ in data if not t)}")


def hold_ret(b):
    won = b["outcome"] == b["side"]
    cost = b["ask"] + fee(b["ask"])
    return (1.0 if won else 0.0) / cost - 1


# A sale fills a moment after the rule fires, and the book often moves in that moment: against the 15 live strike-through sales the tape bid at the trigger was off by 7.7c on average, the tape bid one second
# later by 1.8c. So the default prices a sale at the bid DELAY seconds after the trigger with SLIP on top; SLIP=0.02 DELAY=0 is the older, optimistic setting.
DELAY = float(os.environ.get("DELAY", "1.0"))
SLIP = float(os.environ.get("SLIP", "0.0"))


def sale_ret(b, bid):
    cost = b["ask"] + fee(b["ask"])
    return (bid - SLIP - fee(bid)) / cost - 1


def simulate(b, ticks, qts, bids, line, stay, window, pause_frac=0.5, rearm=False, rearm_left=0, second=None):
    """The (time left, bid, return) of the sale, or None if the rule holds to the close."""
    through_on = False
    since_t = 0.0
    worst = best = best_ts = 0.0
    bounce = False
    kept = False  # came back once; with `second` = (stay, minimum bid), a second stretch past the line that lasts `stay` seconds sells if the bid is still `minimum` or more
    since2 = 0.0
    for ts, v in ticks:
        left = b["close_ts"] - ts
        if left <= 3:
            return None
        th = (b["strike"] - v) / b["strike"] if b["side"] == "yes" else (v - b["strike"]) / b["strike"]
        if kept:
            if th >= line:
                since2 = since2 or ts
                if ts - since2 >= second[0]:
                    i = bisect.bisect_right(qts, ts) - 1
                    if i >= 0 and ts - qts[i] <= 5 and second[1] <= bids[i] < 0.99:
                        return left, bids[i], sale_ret(b, bids[i])
            else:
                since2 = 0.0
            continue
        if th <= 0:
            if through_on:
                if second is not None:
                    kept, since2 = True, 0.0
                    continue
                if not (rearm or left <= rearm_left):
                    return None
                through_on, since_t, bounce = False, 0.0, False
            continue
        if th >= line:
            if since_t <= 0:
                since_t = ts
        else:
            since_t = 0.0
        stayed = since_t > 0 and ts - since_t >= stay
        if not through_on:
            if th < line:
                continue
            through_on, worst, best, best_ts, bounce = True, th, th, ts, False
            continue
        in_win = window > 0 and left <= window
        fell = False
        if in_win:
            if th < best:
                best, best_ts, bounce = th, ts, True
            else:
                fell = bounce and th > best
                if not fell and th > worst:
                    worst, best, best_ts, bounce = th, th, ts, False
        if not stayed and not (in_win and (fell or ts - best_ts >= window * pause_frac)):
            continue
        i = bisect.bisect_right(qts, ts) - 1
        if i < 0 or ts - qts[i] > 5:
            continue
        if bids[i] >= 0.99:
            continue
        j = bisect.bisect_right(qts, ts + DELAY) - 1  # the bid the sale actually meets
        bid = bids[max(i, j)]
        return left, bid, sale_ret(b, bid)
    return None


def run(line, stay, window, rearm=False, rearm_left=0, second=None):
    n = won_anyway = 0
    sold_usd = held_usd = 0.0
    for b, ticks, qts, bids in data:
        r = simulate(b, ticks, qts, bids, line, stay, window, 0.5, rearm, rearm_left, second)
        h = hold_ret(b)
        if r is None:
            sold_usd += b["stake_usd"] * h
            held_usd += b["stake_usd"] * h
        else:
            n += 1
            won_anyway += b["outcome"] == b["side"]
            sold_usd += b["stake_usd"] * r[2]
            held_usd += b["stake_usd"] * h
    return n, won_anyway, sold_usd, held_usd


actual = sum(b["stake_usd"] * (sale_ret(b, b["exit_price"]) if b["exit_price"] is not None else hold_ret(b)) for b, *_ in data)
print(f"\nactual P&L of these bets ${actual:+.2f}; held all to the close ${sum(b['stake_usd'] * hold_ret(b) for b, *_ in data):+.2f}\n")
print(f"{'line%':>7} {'stay':>5} {'win':>4} {'sold':>5} {'wouldwin':>8} {'P&L $':>9} {'vs hold':>9}")
rows = []
for line, stay, window in itertools.product((0.003, 0.005, 0.008), (20, 30, 45, 60), (0, 60, 90, 120, 180, 240)):
    n, wa, pl, hold = run(line / 100.0, stay, window)
    rows.append((pl, line, stay, window, n, wa, hold))
for pl, line, stay, window, n, wa, hold in sorted(rows, reverse=True)[:10] + [r for r in rows if (r[1], r[2]) == (0.005, 45)]:
    print(f"{line:>7} {stay:>5} {window:>4} {n:>5} {wa:>8} {pl:>+9.2f} {pl - hold:>+9.2f}")

print("\nbets that ended as a loss (held or sold) under the current rule 0.003 / 45 / 60:")
print(f"{'entered':>11} {'tk':<8} {'side':<4} {'ask':>5} {'left0':>5}  actual                 replay            max-through%  first>=0.003 left")
for b, ticks, qts, bids in data:
    r = simulate(b, ticks, qts, bids, 0.003 / 100, 45, 60)
    won = b["outcome"] == b["side"]
    if won and (r is None or r[2] > 0) and b["exit_price"] is None:
        continue
    th = [((b["strike"] - v) / b["strike"] if b["side"] == "yes" else (v - b["strike"]) / b["strike"], ts) for ts, v in ticks]
    mx = max(th, key=lambda x: x[0])
    first = next((b["close_ts"] - ts for t, ts in th if t >= 0.003 / 100), None)
    act = f"sold {b['exit_price']:.2f}" if b["exit_price"] is not None else ("held won" if won else "held LOST")
    rep = "hold" if r is None else f"sold {r[1]:.2f} @{r[0]:.0f}s"
    print(f"{datetime.fromtimestamp(b['ts'], timezone.utc):%d %H:%M:%S} {b['series'][2:5]:<8} {b['side']:<4} {b['ask']:>5.2f} {b['close_ts'] - b['ts']:>5.0f}  {act:<22} {rep:<17} {mx[0] * 100:>8.4f}      {'' if first is None else f'{first:.0f}s'}")

print("\nwider last-minute windows and pause shares (line 0.003 / stay 45):")
for window in (60, 90, 120, 180, 240):
    for pf in (0.25, 0.5):
        n = 0; pl = 0.0
        for b, ticks, qts, bids in data:
            r = simulate(b, ticks, qts, bids, 0.003 / 100, 45, window, pf)
            n += r is not None
            pl += b["stake_usd"] * (hold_ret(b) if r is None else r[2])
        print(f"window {window:>3} pause x{pf}: sold {n:>3}  P&L {pl:+.2f}")

print("\nre-arming the watch after a return to the winning side (line / stay / window -> hold-after-return vs re-arm):")
for line, stay, window in ((0.003, 45, 60), (0.003, 30, 60), (0.005, 45, 60), (0.003, 20, 60), (0.005, 20, 60)):
    a = run(line / 100, stay, window, False); r = run(line / 100, stay, window, True)
    print(f"{line:>6} {stay:>3} {window:>3}: hold-after-return sold {a[0]:>2} {a[2]:+8.2f} | re-arm sold {r[0]:>2} (would have won {r[1]}) {r[2]:+8.2f}")

print("\nre-arm only when the return happens with this many seconds left or fewer (line 0.003 / stay 45 / window 60; 0 = never):")
for rl in (0, 60, 90, 120, 180, 240):
    for line in (0.003, 0.005):
        n, wa, pl, hold = run(line / 100, 45, 60, False, rl)
        print(f"  rearm_left {rl:>3} line {line}: sold {n:>2} (would have won {wa}) P&L {pl:+8.2f}")

print("\nsecond stretch after a return sells only after a longer stay with the bid still above a floor (line / first stay 45 / window 60):")
print(f"  {'line':>6} {'stay2':>5} {'min bid':>7} {'sold':>5} {'would-win':>9} {'P&L $':>9}")
base = run(0.003 / 100, 45, 60)
print(f"  {'now':>6} {'-':>5} {'-':>7} {base[0]:>5} {base[1]:>9} {base[2]:>+9.2f}")
for line in (0.003, 0.005):
    for stay2 in (30, 45, 60, 90):
        for minbid in (0.0, 0.15, 0.25, 0.35):
            n, wa, pl, hold = run(line / 100, 45, 60, False, 0, (stay2, minbid))
            print(f"  {line:>6} {stay2:>5} {minbid:>7} {n:>5} {wa:>9} {pl:>+9.2f}")

print("\nthe last-minute watch over a wider window, with the pause that sells held at a fixed number of seconds (line 0.005 / stay 45; fill as set by DELAY and SLIP):")
print(f"  {'window':>6} {'pause s':>7} {'sold':>5} {'would-win':>9} {'P&L $':>9}")
for window in (60, 90, 120, 180):
    for pause in (15, 30, 45, 60):
        n = wa = 0
        pl = 0.0
        for b, ticks, qts, bids in data:
            r = simulate(b, ticks, qts, bids, 0.005 / 100, 45, window, pause / window)
            n += r is not None
            wa += r is not None and b["outcome"] == b["side"]
            pl += b["stake_usd"] * (hold_ret(b) if r is None else r[2])
        print(f"  {window:>6} {pause:>7} {n:>5} {wa:>9} {pl:>+9.2f}")
