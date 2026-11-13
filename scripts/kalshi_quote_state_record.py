"""Would a state record read from Kalshi's own quote curve have picked the right bets? (2026-11-13, user: "switch to use kalshis price instead of the external feeds for everything"; the exit went first, this is the buy side.)

Question, one: on the live (trade-mode) 15-minute bets the bot placed since Monday 2026-11-09 00:00 ET, what would buying only where the Kalshi-curve state record says the state paid have done against what those bets actually got (the lane's own exit or the settlement, as booked)?

The state of a market side at a moment, with no spot feed and no market name in it:
  quote layers, 2 and 5 minutes: the side's mid (bid+ask over 2 of the tape `quotes`) on a one-second grid, fitted with the curve model's parabola (curvemodel._PARABOLA) in that window's own typical move (the root-mean-square one-second move of the mid times sqrt(window seconds)).
    level   |mid - 0.5| at 5 minutes in those units, a tercile 1/2/3 (1 = near even in the market's own variance, 3 = far)
    slope   per window: u (toward the side), f, d, by the tercile cut of the market's own slopes
    bend    per window: p (the path bends toward the side) or n (away), the sign of the curvature
    z       a 2-minute window where the mid did not move at all; "still" when it did not move for 5 minutes
  long layer: the Kalshi perp reference (index_ticks KP_*, watcher.perp_index) over the last hour, its slope in its own typical hour: U (toward the side), F, D by the same tercile rule.
  Written "3up.fn.U" = level 3, 2-minute slope up bending toward, 5-minute flat bending away, perp hour toward the side.
Cuts (rule 2): each market's own readings of the 24 hours before the top of the hour, recomputed hourly; under 300 readings = unread. The two sides mirror each other, so a tercile cut is +/- the 33rd percentile of the absolute reading.
Anomaly (rule 3): any reading beyond its market's 1st/99th percentile (the 98th of the absolute reading), or a typical move over three times its median, is the state "anomaly": never in the record, never bought, counted on its own line.
The record (rule 8, as staterecord.py): every settled 15-minute window that opened since Monday, read every 30 s from minute 5 to 14, both sides, ask 0.05-0.95 and spread at most 0.04; one reading per market side, state and price band; the net per $1 staked held to the result after the taker fee; shrunk by K = 10 readings. Price bands: under 0.35, 0.35-0.5, 0.5-0.7, 0.7-0.8, 0.8-0.9, 0.9 and over (staterecord's two edges, and the favorites' range in three because a win pays 0.41 per $1 at 0.70 and 0.10 at 0.90).
Walk-forward: a bet is judged by the record of the markets that closed before the top of the bet's hour, nothing later. Nothing before Monday is counted, so the first hours have no record and the rule sits out there.
The rule: keep the bet when its state at the buy has at least 15 earlier markets at the bet's price band (the lane default record_min_n) and a shrunk net above zero; otherwise do not buy ($0). The lane default record_min 0.20 cannot be reached at favorite prices, so the bar is zero, said before the run.
  R1 (the question): the state with the perp hour.   R2 (a check, said before the run: what the perp layer adds): the quote layers alone.
Dollars: what the bet booked. Cost = ask + fee; a sale at the booked exit price less the 0.02 slip and the taker fee (a resting-ask fill at its own price, no fee); unsold = the settlement. A kept bet is a tie; a skipped loser is a bet the rule beat; a skipped winner is one it lost to.
Qualifies for a shadow lane (fixed before the run): R1 keeps at least 30 bets, the change against what happened is positive, the kept bets net positive, and the kept bets are positive in every shape (each letter of each layer) where it kept at least 5.
    scripts/cloud_job.sh scripts/kalshi_quote_state_record.py
"""

import bisect
import calendar
import collections
import datetime as dt
import itertools
import math
import os

from kalshi_moonshot import config, db
from kalshi_moonshot.curvemodel import _PARABOLA, QUAD_SAMPLES
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.watcher import perp_index

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
ET = dt.timezone(dt.timedelta(hours=-5))
# Scope (user, 2026-11-13: "it should only be crypto multiscale, and commodity favorite lanes"; the underdog was live only 11-09 13:47 to 11-11 05:37 ET and is out, commodity_multiscale is out too).
SCOPE_LANES = ("crypto_multiscale", "commodity_energy", "commodity_wti", "commodity_natgas", "commodity_precious", "commodity_industrial")
LANE_SQL = ",".join("'%s'" % x for x in SCOPE_LANES)

hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731

MON = calendar.timegm((2026, 11, 9, 5, 0, 0))  # 11-09 00:00 ET
LOOKS = (120, 300)
HOUR = 3600
K = 10.0
MIN_N = 15
MIN_HIST = 300
BANDS = (0.35, 0.5, 0.7, 0.8, 0.9)
STEP = 30
CAP = 0.04
FIELDS = ("lvl", "s2", "c2", "s5", "c5")


def band(ask):
    return bisect.bisect_right(BANDS, ask)


BAND_NAMES = ["<0.35", "0.35-0.5", "0.5-0.7", "0.7-0.8", "0.8-0.9", ">=0.9"]


def quant(sorted_vals, p):
    return sorted_vals[min(len(sorted_vals) - 1, int(p * len(sorted_vals)))]


def read_quote(mid, sq, k):
    """The YES-side readings at second k of a window's mid grid: lvl, s2, c2, s5, c5 and the two units; None when the 5 minutes are not covered. A window with no movement has unit None."""
    out = {}
    for look, tag in zip(LOOKS, ("2", "5"), strict=True):
        if k - look < 0 or mid[k - look] is None or mid[k] is None:
            return None
        rate = math.sqrt(max(sq[k] - sq[k - look], 0.0) / look)
        if rate <= 0:
            out["u" + tag] = None
            continue
        unit = rate * math.sqrt(look)
        ys = [(mid[k - look + round(i * look / (QUAD_SAMPLES - 1))] - 0.5) / unit for i in range(QUAD_SAMPLES)]
        a, b, c = (sum(w[i] * ys[i] for i in range(QUAD_SAMPLES)) for w in _PARABOLA)
        out["u" + tag] = unit
        out["s" + tag], out["c" + tag] = b, c
        if tag == "5":
            out["lvl"] = a
    return out


def perp_grid(ix, g0, g1):
    """One-second log grid of a perp reference (None where the last tick is over 120 s old) and the running sum of squared steps."""
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, g0 - 120)).fetchall()
    ts = [float(x[0]) for x in r]
    vs = [float(x[1]) for x in r]
    g, j, last, last_t = [], 0, None, 0.0
    for t in range(g0, g1):
        while j < len(ts) and ts[j] <= t:
            last, last_t = math.log(vs[j]), ts[j]
            j += 1
        g.append(last if last is not None and t - last_t <= 120 else None)
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    return g, sq, (ts[0] if ts else None)


def read_perp(pg, t, g0):
    if pg is None:
        return None
    g, sq, _ = pg
    k = int(t) - g0
    if k - HOUR < 0 or k >= len(g) or g[k] is None or g[k - HOUR] is None:
        return None
    rate = math.sqrt(max(sq[k] - sq[k - HOUR], 0.0) / HOUR)
    if rate <= 0:
        return None
    return (g[k] - g[k - HOUR]) / (rate * math.sqrt(HOUR)), rate


class Cuts:
    """A market's own cuts, from its readings of the 24 hours before the top of the hour."""

    def __init__(self):
        self.t, self.rows, self.pt, self.prows, self.cache = [], [], [], [], {}

    def add(self, t, rd, pp):
        if rd.get("u5") is not None:
            self.t.append(t)
            self.rows.append(rd)
        if pp is not None:
            self.pt.append(t)
            self.prows.append(pp)

    def at(self, t):
        h = int(t // HOUR) * HOUR
        if h in self.cache:
            return self.cache[h]
        out = {}
        rows = self.rows[bisect.bisect_left(self.t, h - 86400) : bisect.bisect_left(self.t, h)]
        if len(rows) >= MIN_HIST:
            for f in FIELDS:
                v = sorted(abs(r[f]) for r in rows if r.get(f) is not None)
                if len(v) < MIN_HIST // 2:
                    out = {}
                    break
                out[f] = (quant(v, 1 / 3), quant(v, 2 / 3), quant(v, 0.98))
            if out:
                u = sorted(r["u5"] for r in rows)
                out["u5"] = u[len(u) // 2]
        prow = self.prows[bisect.bisect_left(self.pt, h - 86400) : bisect.bisect_left(self.pt, h)]
        if len(prow) >= MIN_HIST:
            v = sorted(abs(p[0]) for p in prow)
            r = sorted(p[1] for p in prow)
            out["perp"] = (quant(v, 1 / 3), quant(v, 0.98), r[len(r) // 2])
        self.cache[h] = out
        return out


def letters(rd, pp, cuts, sign):
    """(the quote state, the perp letter): the quote state is None when unread, "anomaly", "still" or the five letters; the perp letter is None when unread, "!" for an anomaly, or U/F/D."""
    if rd is None or not cuts or "lvl" not in cuts:
        qs = None
    elif rd.get("u5") is None:
        qs = "still"
    elif rd["u5"] > 3.0 * cuts["u5"] or any(rd.get(f) is not None and abs(rd[f]) > cuts[f][2] for f in FIELDS):
        qs = "anomaly"
    else:
        lv = abs(rd["lvl"])
        L = "1" if lv < cuts["lvl"][0] else "2" if lv < cuts["lvl"][1] else "3"
        parts = []
        for tag in ("2", "5"):
            if rd.get("u" + tag) is None:
                parts.append("zz")
                continue
            sl, cv = sign * rd["s" + tag], sign * rd["c" + tag]
            c = cuts["s" + tag][0]
            parts.append(("u" if sl > c else "d" if sl < -c else "f") + ("p" if cv >= 0 else "n"))
        qs = f"{L}{parts[0]}.{parts[1]}"
    if pp is None or not cuts or "perp" not in cuts:
        pl = None
    elif abs(pp[0]) > cuts["perp"][1] or pp[1] > 3.0 * cuts["perp"][2]:
        pl = "!"
    else:
        v = sign * pp[0]
        pl = "U" if v > cuts["perp"][0] else "D" if v < -cuts["perp"][0] else "F"
    return qs, pl


def full_state(qs, pl):
    """R1's state: the quote state with the perp letter; "anomaly" when either is one; None when either is unread."""
    if qs == "anomaly" or pl == "!":
        return "anomaly"
    if qs is None or pl is None:
        return None
    return f"{qs}.{pl}"


def dollars(r):
    ask, stake = float(r["ask"]), float(r["stake_usd"])
    cost = ask + fee(ask)
    if r["exit_price"] is not None:
        px = float(r["exit_price"])
        back = px if str(r["exit_reason"] or "").startswith("curve exit (resting ask)") else px - EXIT_SLIP - fee(px)
    elif r["result"] in ("yes", "no"):
        back = 1.0 if r["result"] == r["side"] else 0.0
    else:
        return None
    return stake * (back / cost - 1.0)


# ---- the bets ----
bet_rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.ask,d.stake_usd,d.exit_price,d.exit_reason,w.close_ts,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.stake_usd>0 AND d.ask>0 AND d.ts>? AND d.ticker LIKE '%15M-%' AND d.lane IN (" + LANE_SQL + ") AND w.result IN ('yes','no') ORDER BY d.ts",
    (MON,),
).fetchall()
bets_by_ticker = collections.defaultdict(list)
for r in bet_rows:
    bets_by_ticker[r["ticker"]].append(r)

wins = st.db.execute("SELECT ticker,series,close_ts,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND ticker LIKE '%15M-%' ORDER BY series,close_ts", (MON + 900,)).fetchall()
by_series = collections.defaultdict(list)
for w in wins:
    by_series[w["series"]].append(w)
t_end = max(float(w["close_ts"]) for w in wins)
print(f"settled 15-minute windows opened since Monday 11-09 00:00 ET: {len(wins)} in {len(by_series)} markets; the restored tape's last window closes {hm(t_end)} ET")
print(f"live trade-mode bets since Monday, settled: {len(bet_rows)}")

G0 = MON - HOUR - 300
G1 = int(t_end) + 2
entries = []  # (close, ticker, side, quote state, perp letter, band, net per $1)
judged = []  # (bet row, quote state, perp letter, readings)
markets = 0
unread_why = collections.Counter()
perp_first = []
tally = collections.Counter()
for series in sorted(by_series):
    kp = perp_index(series)
    pg = perp_grid(kp, G0, G1) if kp else None
    if pg and pg[2]:
        perp_first.append(pg[2])
    cuts = Cuts()
    pending = []  # this market's readings, lettered after its cuts are complete
    for w in by_series[series]:
        close = float(w["close_ts"])
        opened = close - 900.0
        q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
        if len(q) < 20:
            continue
        qt = [float(x[0]) for x in q]
        mid, j, last = [], 0, None
        for sec in range(901):
            while j < len(qt) and qt[j] <= opened + sec:
                last = (float(q[j][1]) + float(q[j][2])) / 2.0
                j += 1
            mid.append(last)
        sq = [0.0]
        for a, b in itertools.pairwise(mid):
            sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
        markets += 1
        for e in range(300, 841, STEP):
            at = opened + e
            rd = read_quote(mid, sq, e)
            pp = read_perp(pg, at, G0)
            if rd is None:
                continue
            cuts.add(at, rd, pp)
            i = bisect.bisect_right(qt, at) - 1
            if i < 0 or at - qt[i] > 20:
                continue
            pending.append(("rec", at, w, rd, pp, float(q[i][1]), float(q[i][2])))
        for b in bets_by_ticker.get(w["ticker"], ()):
            e = int(float(b["ts"]) - opened)
            rd = read_quote(mid, sq, e) if 0 <= e <= 900 else None
            pending.append(("bet", float(b["ts"]), b, rd, read_perp(pg, float(b["ts"]), G0), float(e), 0.0))
    for kind, at, w, rd, pp, yb, ya in pending:
        c = cuts.at(at)
        if kind == "bet":
            qs, pl = letters(rd, pp, c, 1 if w["side"] == "yes" else -1)
            why = "" if qs is not None else "bought before minute 5 (the 5-minute layer is not covered)" if yb < 300 else "no quote path on the tape" if rd is None else "the market has under 300 readings in its last 24 h (new since Monday, or thinly quoted)"
            judged.append((w, qs, pl, why))
            unread_why[why] += 1
            if qs is not None and pl is None:
                unread_why["quote state read, perp hour unread (no perp, or before 11-11 08:45 ET)"] += 1
            continue
        for side, sign, bid, ask in (("yes", 1, yb, ya), ("no", -1, 1 - ya, 1 - yb)):
            if not (0.05 <= ask <= 0.95) or ask - bid > CAP:
                continue
            qs, pl = letters(rd, pp, c, sign)
            tally["readings"] += 1
            if qs is None:
                tally["unread (under 300 readings in the market's last 24 h)"] += 1
                continue
            if full_state(qs, pl) == "anomaly":
                tally["anomaly"] += 1
            if pl is None:
                tally["perp hour unread"] += 1
            won = w["result"] == side
            net = ((1 - ask) if won else -ask) / ask - fee(ask) / ask
            entries.append((float(w["close_ts"]), w["ticker"], side, qs, pl, band(ask), net))

print(f"markets read: {markets}; record moments (side, ask 0.05-0.95, spread <= {CAP}): {tally['readings']}")
for k in ("unread (under 300 readings in the market's last 24 h)", "anomaly", "perp hour unread"):
    print(f"   {k}: {tally[k]}")
if perp_first:
    print(f"perp reference ticks on the tape begin {hm(min(perp_first))} to {hm(max(perp_first))} ET across {len(perp_first)} markets (the hour layer needs an hour after that)")

# ---- walk-forward: each bet judged by the record of markets closed before the top of its hour ----
entries.sort(key=lambda x: x[0])
judged.sort(key=lambda x: float(x[0]["ts"]))
cells = {1: {}, 2: {}}
seen = {1: set(), 2: set()}
ptr = 0


def feed(until):
    global ptr
    while ptr < len(entries) and entries[ptr][0] <= until:
        close, ticker, side, qs, pl, bd, net = entries[ptr]
        ptr += 1
        for rule, state in ((1, full_state(qs, pl)), (2, qs)):
            if state in (None, "anomaly"):
                continue
            key = (ticker, side, state, bd)
            if key in seen[rule]:
                continue
            seen[rule].add(key)
            cell = cells[rule].setdefault((state, bd), [0, 0.0])
            cell[0] += 1
            cell[1] += net


results = []  # (row, dollars, quote state, perp letter, {rule: (verdict, n, shrunk)})
for b, qs, pl, _ in judged:
    d = dollars(b)
    if d is None:
        continue
    feed(int(float(b["ts"]) // HOUR) * HOUR)
    out = {}
    for rule, state in ((1, full_state(qs, pl)), (2, qs)):
        if state is None:
            out[rule] = ("unread", 0, 0.0)
        elif state == "anomaly":
            out[rule] = ("anomaly", 0, 0.0)
        else:
            n, tot = cells[rule].get((state, band(float(b["ask"]))), (0, 0.0))
            shr = tot / (n + K)
            out[rule] = ("keep" if n >= MIN_N and shr > 0 else "thin record" if n < MIN_N else "record not positive", n, shr)
    results.append((b, d, qs, pl, out))
feed(float("inf"))


def line(rows):
    stake = sum(float(r[0]["stake_usd"]) for r in rows)
    tot = sum(r[1] for r in rows)
    wins_ = sum(1 for r in rows if r[1] > 0)
    return f"n={len(rows):4d} | {tot:+8.2f} on ${stake:8.2f} ({(tot / stake if stake else 0):+.1%}) | {wins_} ended up"


print(f"bets read: {len(results)} of {len(bet_rows)} (the rest have no quote path on the restored tape or a window that opened before Monday)")
for why_, n_ in sorted(unread_why.items()):
    if why_:
        print(f"   {n_:4d} bets: {why_}")
print(f"\nWHAT HAPPENED on the same bets: {line(results)}")
lanes_ = collections.defaultdict(list)
for r in results:
    lanes_[r[0]["lane"]].append(r)
for name in sorted(lanes_):
    print(f"   {name:24s} {line(lanes_[name])}")

verdict = {}
for rule, label in ((1, "R1  quote layers + perp hour (the question)"), (2, "R2  quote layers alone (check)")):
    kept = [r for r in results if r[4][rule][0] == "keep"]
    skipped = [r for r in results if r[4][rule][0] != "keep"]
    change = -sum(r[1] for r in skipped)
    beat = sum(1 for r in skipped if r[1] < 0)
    lost = sum(1 for r in skipped if r[1] > 0)
    print(f"\n{label}")
    print(f"   kept            {line(kept)}")
    print(f"   net change against what happened: {change:+.2f} | bets the rule beat (skipped losers) {beat}, lost to (skipped winners) {lost}, same (kept or flat) {len(results) - beat - lost}")
    for why in ("unread", "anomaly", "thin record", "record not positive"):
        grp = [r for r in results if r[4][rule][0] == why]
        print(f"   skipped, {why:20s} {line(grp)}")
    print(f"   record at the tape's end: {len(cells[rule])} state-and-band cells, {sum(1 for n, t in cells[rule].values() if n >= MIN_N)} with {MIN_N}+ markets, {sum(1 for n, t in cells[rule].values() if n >= MIN_N and t / (n + K) > 0)} of those positive")
    # by curve shape: each letter of each layer
    shapes = [("level (5m, own variance)", lambda r: r[2][0] if r[2] and r[2][0] in "123" else None), ("2m slope", lambda r: r[2][1] if r[2] and "." in r[2] else None), ("2m bend", lambda r: r[2][2] if r[2] and "." in r[2] else None),
              ("5m slope", lambda r: r[2][4] if r[2] and "." in r[2] else None), ("5m bend", lambda r: r[2][5] if r[2] and "." in r[2] else None), ("perp hour", lambda r: r[3])]
    if rule == 2:
        shapes = shapes[:-1]
    ok_shapes = True
    print("   by curve shape (the state's own letters at the buy): all bets | kept")
    for name, get in shapes:
        vals = sorted({get(r) for r in results if get(r) is not None})
        for v in vals:
            allv = [r for r in results if get(r) == v]
            kv = [r for r in kept if get(r) == v]
            ktot = sum(r[1] for r in kv)
            flag = ""
            if len(kv) >= 5 and ktot <= 0:
                ok_shapes = False
                flag = "  <- kept bets not positive"
            print(f"      {name:26s} {v}: all n={len(allv):4d} {sum(r[1] for r in allv):+8.2f} | kept n={len(kv):4d} {ktot:+8.2f} | skipped {-sum(r[1] for r in allv if r not in kv):+8.2f} change{flag}")
    print("   by price band: all bets | kept")
    for bd in range(len(BAND_NAMES)):
        allv = [r for r in results if band(float(r[0]["ask"])) == bd]
        if allv:
            kv = [r for r in kept if band(float(r[0]["ask"])) == bd]
            print(f"      {BAND_NAMES[bd]:9s} all n={len(allv):4d} {sum(r[1] for r in allv):+8.2f} | kept n={len(kv):4d} {sum(r[1] for r in kv):+8.2f}")
    by_state = collections.defaultdict(list)
    for r in kept:
        by_state[full_state(r[2], r[3]) if rule == 1 else r[2]].append(r)
    print("   kept bets by state (3 or more bets):")
    for state, grp in sorted(by_state.items(), key=lambda kv: -len(kv[1])):
        if len(grp) >= 3:
            print(f"      {state:12s} n={len(grp):3d} {sum(r[1] for r in grp):+7.2f}  (record behind them at the buy: {min(r[4][rule][1] for r in grp)}-{max(r[4][rule][1] for r in grp)} markets, shrunk {min(r[4][rule][2] for r in grp):+.3f} to {max(r[4][rule][2] for r in grp):+.3f} per $1)")
    ktot = sum(r[1] for r in kept)
    checks = [(f"kept at least 30 bets ({len(kept)})", len(kept) >= 30), (f"change against what happened positive ({change:+.2f})", change > 0), (f"kept bets net positive ({ktot:+.2f})", ktot > 0), ("kept bets positive in every shape with 5+ kept", ok_shapes and len(kept) >= 5)]
    for text, ok in checks:
        print(f"   [{'x' if ok else ' '}] {text}")
    verdict[rule] = all(ok for _, ok in checks)

print(f"\nVERDICT: R1 {'QUALIFIES for a shadow lane (the live switch stays the user\'s)' if verdict[1] else 'does NOT qualify'}; R2 (check only) {'meets the same bar' if verdict[2] else 'does not meet the bar'}.")
print("DONE")
