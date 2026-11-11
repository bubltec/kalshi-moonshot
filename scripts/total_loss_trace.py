"""Why did no exit catch the bets held to a total loss? (2026-11-11, user: "Yes, trace them".)
Question: over the live crypto_multiscale and commodity_multiscale bets of the last 18 hours that settled, 12 of 109 were held to a total loss (no sale, side lost). The two existing sales that could have cut them are off on
both lanes (switched off on 11-11): time-to-go and market-disagrees. On THE SAME bets, what would each have done, what would it have cost on the bets that did not need it, and in two halves of the period?

Rules, exactly the settings those sales had (fixed before results; nothing swept):
  time-to-go  with 240 s or fewer left (and 8 s or more), a position whose curve chance is under 0.5 is sold at the bid.
  gap         the curve chance is at least 0.35 over the bid (after the sell fee) for 15 s: sold at the bid.
  either      whichever fires first.
The curve chance is the lane's own reading: Phi(cushion / (rate * sqrt(seconds left))), rate = rms of 60-s log moves over 900 s / sqrt(60), scale 900 (curvevalue.read). Cushion against the strike for a coin (the strike taken as the
index's mean over the 60 s before the window opened, checked against the strike the bot logged for NEAR: 3.7360); against the index's own value at the window open for a commodity, as the lane reads it.
A rule fires from the second after the buy; a bet the lane's own sale already closed keeps that sale unless the rule fired first. Sales at the bid less the sell fee; dollars use each bet's stake_usd; the cost is ask plus entry fee.
Reported: dollars as it happened, with each rule, the bets changed, helped and hurt, the 12 total losses one by one, and the two halves. Curve-shape groups are not split: a hundred bets cannot carry it.

    scripts/cloud.sh run scripts/total_loss_trace.py
"""

import bisect
import datetime as dt
import math
import os
import re
import time

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
ET = dt.timezone(dt.timedelta(hours=-5))
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
now = time.time()
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%H:%M:%S")  # noqa: E731


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def close_of(tk):
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", tk)
    return dt.datetime(2000 + int(m[1]), MON[m[2]], int(m[3]), int(m[4]), int(m[5]), tzinfo=ET).timestamp()


def build(r):
    """Per-second (second, seconds left, our side's bid, curve chance or None) from the buy to the close, or None."""
    tk, ser = r["ticker"], r["ticker"].split("-")[0]
    close, yes = close_of(tk), (r["side"] or "yes") == "yes"
    t0 = float(r["ts"])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (tk, t0 - 5, close)).fetchall()
    if len(q) < 5:
        return None
    qt = [float(x[0]) for x in q]
    bids = []
    for t in range(int(t0) + 1, int(close)):
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        bids.append([t, close - t, float(q[i][1]) if yes else 1.0 - float(q[i][2]), None])
    if len(bids) < 5:
        return None
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        return bids
    rows = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, close - 900 - 1000, close)).fetchall()
    if len(rows) < 1200:
        return bids
    T = [float(x[0]) for x in rows]
    base = int(close) - 1900
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T) and T[j] <= t:
            last = float(rows[j][1])
            j += 1
        g.append(math.log(last) if last else None)
    k_open = int(close) - 900 - base
    if crypto:
        win = [x for x in g[k_open - 60:k_open + 1] if x is not None]
        ref = sum(win) / len(win) if win else None  # the index's mean over the 60 s before the open
    else:
        ref = g[k_open]
    if ref is None or any(x is None for x in g[40:]):
        return bids
    c60 = [0.0] * len(g)
    for k in range(100, len(g)):
        c60[k] = c60[k - 1] + (g[k] - g[k - 60]) ** 2 / 60.0
    sign = 1.0 if yes else -1.0
    for b in bids:
        k = int(b[0]) - base
        if k - 900 >= 100 and b[1] > 0:
            rate = math.sqrt(max(c60[k] - c60[k - 900], 0.0) / 900.0)
            c = (g[k] - ref) * sign
            b[3] = phi(c / (rate * math.sqrt(b[1]))) if rate > 0 else (1.0 if c > 0 else 0.0)
    return bids


def fire(bids, rule):
    """(second, bid) when the rule first fires, else None."""
    since = None
    for t, left, bid, ch in bids:
        if ch is None or left < 8 or bid <= 0:
            since = None
            continue
        if rule in ("time-to-go", "either") and left <= 240.0 and ch < 0.5:
            return t, bid
        if rule in ("gap", "either"):
            if ch - (bid - fee(bid)) >= 0.35:
                since = since if since is not None else t
                if t - since >= 15:
                    return t, bid
            else:
                since = None
    return None


def dollars(r, sale):
    """Dollars for the bet: sale = (price net of fee) or None to settle."""
    ask, stake = float(r["ask"]), float(r["stake_usd"] or 0.0)
    cost = ask + fee(ask)
    won = 1.0 if r["outcome"] == (r["side"] or "yes") else 0.0
    back = won if sale is None else sale
    return stake * (back / cost - 1.0)


def actual(r):
    if r["exit_price"] is None:
        return dollars(r, None), None
    px = float(r["exit_price"])
    return dollars(r, px - 0.02 - fee(px)), float(r["exit_ts"])


LANES = ("crypto_multiscale", "commodity_multiscale")
rows = st.db.execute("SELECT id, ts, lane, ticker, side, ask, stake_usd, outcome, exit_price, exit_ts, exit_reason FROM decisions WHERE action='bet' AND lane IN (?,?) AND ts>? AND outcome IS NOT NULL ORDER BY ts", (*LANES, now - float(os.environ.get("HOURS", "18")) * 3600)).fetchall()
rows = [r for r in rows if float(r["ts"]) <= now - float(os.environ.get("UNTIL", "0")) * 3600]
print(f"{len(rows)} settled live bets in the last {os.environ.get('HOURS', '18')} h; run {hm(now)} ET")
bets = []
for n_, r in enumerate(rows):
    if n_ % 10 == 0:
        print(f"  building paths {n_}/{len(rows)}", flush=True)
    try:
        b = build(r)
    except Exception as exc:  # a path that cannot be built is reported, not fatal
        print(f"  could not build {r['ticker']}: {type(exc).__name__}: {exc}", flush=True)
        continue
    if b is None or float(r["ask"] or 0) <= 0 or float(r["stake_usd"] or 0) <= 0:
        continue
    bets.append((r, b))
print(f"{len(bets)} with a tape path; with a curve chance on at least a minute of it: {sum(1 for _, b in bets if any(x[3] is not None for x in b))}")
nearish = next(((r, b) for r, b in bets if "KXNEAR15M-26NOV111330" in r["ticker"]), None)
RULES = ("time-to-go", "gap", "either")
res = {k: [] for k in RULES}
if os.environ.get("DUMP"):
    for r, b in bets:  # one line a bet so windows run in pieces can be added up locally: lane, ts, stake, as it happened, total loss, then per rule: fired (0/1), dollars with the rule
        a, aexit = actual(r)
        out = [r["lane"], f"{float(r['ts']):.0f}", f"{float(r['stake_usd']):.4f}", f"{a:.4f}", "1" if (r["exit_price"] is None and r["outcome"] != (r["side"] or "yes")) else "0"]
        for rule in RULES:
            f_ = fire(b, rule)
            if f_ is not None and (aexit is None or f_[0] < aexit):
                out += ["1", f"{dollars(r, f_[1] - fee(f_[1])):.4f}"]
            else:
                out += ["0", f"{a:.4f}"]
        print("REC," + ",".join(out), flush=True)

for r, b in bets:
    a, aexit = actual(r)
    for rule in RULES:
        f = fire(b, rule)
        if f is not None and (aexit is None or f[0] < aexit):
            d = dollars(r, f[1] - fee(f[1]) - 0.0)
            res[rule].append((r, a, d, f))
        else:
            res[rule].append((r, a, a, None))
total_loss = [(r, b) for r, b in bets if r["exit_price"] is None and r["outcome"] != (r["side"] or "yes")]
print(f"\nheld to a total loss, with a tape path: {len(total_loss)} of {len(bets)}; as it happened: {sum(actual(r)[0] for r, _ in total_loss):+.2f}")
print("\nlane / time / market / side / paid | what each rule would have done (second left, sale price, dollars)")
for r, b in total_loss:
    have = [x for x in b if x[3] is not None]
    last240 = [x[3] for x in have if x[1] <= 240]
    seen = f"chance read {len(have)}/{len(b)} s" + (f", lowest in the last 240 s {min(last240):.2f}, bid then {min(x[2] for x in b if x[1] <= 240):.2f}" if last240 else ", none in the last 240 s")
    line = f"{r['lane'][:6]} {hm(r['ts'])} {r['ticker'].split('-')[0]:14s} {r['side']:3s} {float(r['ask']):.2f} stake {float(r['stake_usd']):.2f} as it happened {actual(r)[0]:+.2f} | {seen} |"
    for rule in RULES:
        f = fire(b, rule)
        line += f" {rule}: " + (f"{b[[x[0] for x in b].index(f[0])][1]:.0f}s left @ {f[1]:.2f} -> {dollars(r, f[1] - fee(f[1])):+.2f}" if f else "no sale") + " |"
    print(line)
print("\nrule | all bets as it happened | with the rule | change | bets changed (helped / hurt) | caught of the total losses | halves of the change")
tot_a = sum(actual(r)[0] for r, _ in bets)
half_t = sorted(float(r["ts"]) for r, _ in bets)[len(bets) // 2]
for rule in RULES:
    out = res[rule]
    chg = [(r, d - a) for r, a, d, f in out if f is not None]
    helped = sum(1 for _, x in chg if x > 0.005)
    hurt = sum(1 for _, x in chg if x < -0.005)
    caught = sum(1 for r, _ in total_loss if fire(dict_b := next(b for rr, b in bets if rr["id"] == r["id"]), rule) is not None)
    h1 = sum(x for r, x in chg if float(r["ts"]) < half_t)
    h2 = sum(x for r, x in chg if float(r["ts"]) >= half_t)
    new = tot_a + sum(x for _, x in chg)
    print(f"{rule:10s} | {tot_a:+8.2f} | {new:+8.2f} | {new - tot_a:+7.2f} | {len(chg):3d} ({helped} / {hurt}) | {caught} of {len(total_loss)} | {h1:+.2f} / {h2:+.2f}")
for lane in LANES:
    sub = [(r, b) for r, b in bets if r["lane"] == lane]
    print(f"\n{lane}: {len(sub)} bets, as it happened {sum(actual(r)[0] for r, _ in sub):+.2f}")
    for rule in RULES:
        out = [x for x in res[rule] if x[0]["lane"] == lane]
        d = sum(x[2] - x[1] for x in out)
        n = sum(1 for x in out if x[3] is not None)
        print(f"   {rule:10s} change {d:+.2f} on {n} bets it changed")
print("DONE")
