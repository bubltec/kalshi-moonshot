"""The max-loss stop replayed on the bot's own live crypto bets (2026-11-14, user, after a NEAR NO bet bought at 0.56 with 295 s left was sold at 0.29 eighteen seconds later and then won: "A 57c bet should be able to deal with a dip for a moment with 5 mins remaining").
Question: on every live bet of crypto_multiscale and crypto_ladder_perp since 11-13 that settled, what would each stop setting have returned, against what the live stop did and against holding to settlement?
The live rule (watcher._risk_decide): net = bid - fee(bid); basis = entry + fee(entry); pnl = (net - basis) / basis; q = net. Sell when pnl <= -max_loss * q, the bid is at least 0.05, no sale in the last 8 s, and the hour view (curverisk.long_chance of the Kalshi perp, from the real ticks) is under 0.4 or missing. The live setting is max_loss 0.5, no persistence.
Variants (chosen before the run, nothing fitted):
  live          max_loss 0.5, sells at once (what the bot did, replayed: its result is the recorded one)
  persist 0.25  the whole stop condition, hour view included, must hold for 0.25 x the time left (seconds) before the sale; resets when it stops holding
  persist 0.50  the same with 0.50 x the time left
  scaled 300    max_loss = 0.5 x sqrt(time left / 300): looser with more time left, tighter in the last minutes
  scaled 150    max_loss = 0.5 x sqrt(time left / 150)
  max_loss 0.7 / 0.9   a looser flat stop
  no stop       hold to settlement
Sales fill at the bid one second after the decision, net of the sell fee; held bets pay 1 or 0. One entry per bet at the recorded fill price and size.
Reported: totals, bets beaten and lost against the live stop, by curve shape (path efficiency over the 2 minutes before the entry, thirds). Small sample: read it as a check, not a result."""

import bisect
import math

from kalshi_moonshot import config, curverisk, db, quoteshock
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.watcher import perp_index

def swing_hold(drop, unit, left, k):  # curverisk.swing_hold, copied so the script also runs on a host whose image predates it
    return k > 0 and unit is not None and unit > 0 and left > 0 and drop < k * unit * math.sqrt(left / 60.0)


s = config.reload()
main = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
SINCE = 1794470400.0  # 2026-11-13 08:00 ET
CONFIRM = 0.4
GUARD = 8
VARIANTS = [("live now: swing 1.5 + wait 0.5", "swinghold", 1.5), ("cap band 2.5 U", "swingcap", 2.5), ("cap band 3.5 U", "swingcap", 3.5), ("cap band 5 U", "swingcap", 5.0), ("no hold under q 0.30", "swingq", 0.30), ("no hold under q 0.25", "swingq", 0.25), ("no hold under q 0.20", "swingq", 0.20), ("no stop", "off", 0.0)]

bets = main.execute(
    "SELECT id, lane, ticker, side, ts, stake_usd, outcome, exit_ts, exit_price FROM decisions WHERE action='bet' AND mode='trade' AND lane IN ('crypto_multiscale','crypto_ladder_perp') AND ts>? AND outcome IN ('yes','no') ORDER BY ts",
    (SINCE,),
).fetchall()
print(f"live crypto bets that settled since 11-13: {len(bets)}")
rows = []
for b in bets:
    tk, side, t0 = b["ticker"], b["side"], float(b["ts"])
    o = main.execute("SELECT price, fill_count FROM orders WHERE ticker=? AND contract=? AND side='buy' ORDER BY ts LIMIT 1", (tk, side)).fetchone()
    if not o or not o["fill_count"]:
        continue
    entry, n = float(o["price"]), float(o["fill_count"])
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>=0 AND ask>0 ORDER BY ts", (tk, t0 - 1000, t0 + 960)).fetchall()
    if len([r for r in q if r[0] >= t0 - 130]) < 60:
        continue
    import re
    m = re.match(r"^KX[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-", tk)
    close = None
    # the close time comes from the quote tape's last second, not the ticker: use settlement time = first quote tick >= 900 s window end; fall back to t0 + 900
    close = float(q[-1][0]) if q else t0 + 900
    close = max(close, t0 + 60)
    # the window's close is the next quarter hour after entry
    close = (int(t0) // 900 + 1) * 900.0
    grid = list(range(int(t0), int(close)))
    yb, ya, j, last = [], [], 0, None
    for t in grid:
        while j < len(q) and q[j][0] <= t:
            last = (float(q[j][1]), float(q[j][2]))
            j += 1
        yb.append(last[0] if last else None)
        ya.append(last[1] if last else None)
    if any(x is None for x in yb):
        continue
    # the market's own YES mids on a 1 s grid from 1000 s before the entry: the typical minute is read from the last 900 s at each decision
    pre_t0 = int(t0) - 1000
    pm, j2, last2 = [], 0, None
    for t in range(pre_t0, int(t0)):
        while j2 < len(q) and q[j2][0] <= t:
            last2 = (float(q[j2][1]) + float(q[j2][2])) / 2.0
            j2 += 1
        pm.append(last2)
    full = [x for x in pm] + [(yb[k] + ya[k]) / 2.0 for k in range(len(grid))]
    unit_cache = {}

    def unit_at(i, full=full, pre_t0=pre_t0, unit_cache=unit_cache):
        """The market's typical minute at second i of the bet: from the quotes it has had in the last 900 s (a market that has quoted for under 300 s has none, as live)."""
        key = i // 5
        if key not in unit_cache:
            idx = 1000 + i
            lo = max(0, idx - 900)
            while lo <= idx and full[lo] is None:
                lo += 1
            seg = full[lo: idx + 1]
            unit_cache[key] = quoteshock.typical_minute([(float(pre_t0 + lo + n), seg[n]) for n in range(len(seg))], float(pre_t0 + idx)) if len(seg) >= 301 else None
        return unit_cache[key]

    bid = yb if side == "yes" else [1.0 - a for a in ya]
    mids = [(yb[k] + ya[k]) / 2.0 if side == "yes" else 1.0 - (yb[k] + ya[k]) / 2.0 for k in range(len(grid))]
    kp = perp_index(tk.split("-")[0])
    hr = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (kp, close - 900 - 3700, close)).fetchall() if kp else []
    its, ivs = [float(r[0]) for r in hr], [float(r[1]) for r in hr]
    k0 = bisect.bisect_left(its, close - 900)
    ref = ivs[k0] if k0 < len(its) and its[k0] - (close - 900) <= 60 else 0.0
    won = 1.0 if b["outcome"] == side else 0.0
    basis = entry + fee(entry)
    path = sum(abs(mids[k] - mids[k - 1]) for k in range(1, min(120, len(mids))))
    eff = abs(mids[min(119, len(mids) - 1)] - mids[0]) / path if path > 0 else None  # after the entry; the 2 minutes before it are not on this grid
    cache = {}

    def hour_ok(i, its=its, ivs=ivs, ref=ref, close=close, side=side, t0=t0, cache=cache):
        if i not in cache:
            now = t0 + i
            v = curverisk.long_chance(its, ivs, now, ref, side, close - now) if ref > 0 and its else None
            cache[i] = True if v is None else v < CONFIRM
        return cache[i]

    def run(kind, par, bid=bid, grid=grid, basis=basis, close=close, t0=t0, hour_ok=hour_ok, unit_at=unit_at):
        since = None
        for i in range(len(grid) - 3):
            left = close - (t0 + i)
            if left <= GUARD:
                break
            net = bid[i] - fee(bid[i])
            pnl = (net - basis) / basis
            qq = min(max(net, 0.0), 1.0)
            if kind == "off":
                return None
            if kind in ("swing", "swinghold") and pnl < 0 and swing_hold(basis - net, unit_at(i), left, par):
                since = None
                continue
            if kind == "swingcap" and pnl < 0:
                u = unit_at(i)
                if u is not None and u > 0 and (basis - net) < min(1.5 * u * math.sqrt(left / 60.0), par * u):
                    since = None
                    continue
            if kind == "swingq" and pnl < 0 and net >= par and swing_hold(basis - net, unit_at(i), left, 1.5):
                since = None
                continue
            hit = bid[i] >= 0.05 and pnl <= -0.5 * qq and hour_ok(i)
            if not hit:
                since = None
                continue
            if kind in ("persist", "swinghold", "swingcap", "swingq"):
                share = 0.5
                since = i if since is None else since
                if i - since < share * left:
                    continue
            sold = bid[i + 1]
            return sold - fee(sold)
        return None

    def money(sold_net):
        return n * ((sold_net if sold_net is not None else won) - basis)

    live_money = n * ((float(b["exit_price"]) - fee(float(b["exit_price"]))) - basis) if b["exit_ts"] else n * (won - basis)
    res = {name: money(run(kind, par)) for name, kind, par in VARIANTS}
    rows.append({"live": live_money, "hold": n * (won - basis), "res": res, "eff": eff, "stopped": bool(b["exit_ts"])})
print(f"replayed: {len(rows)} (stopped live: {sum(1 for r in rows if r['stopped'])}, held: {sum(1 for r in rows if not r['stopped'])})")


def table(label, sub):
    if len(sub) < 8:
        return
    live, hold = sum(r["live"] for r in sub), sum(r["hold"] for r in sub)
    print(f"\n=== {label}: {len(sub)} bets | live stop {live:+.2f} | hold to settlement {hold:+.2f}")
    for name, _, _ in VARIANTS:
        tot = sum(r["res"][name] for r in sub)
        be = sum(1 for r in sub if r["res"][name] > r["live"] + 1e-6)
        le = sum(1 for r in sub if r["res"][name] < r["live"] - 1e-6)
        print(f"  {name:13s} {tot:+8.2f} | vs live {tot - live:+7.2f} (beat {be}, lost {le}) | vs hold {tot - hold:+7.2f}")


table("all", rows)
effs = sorted(r["eff"] for r in rows if r["eff"] is not None)
if len(effs) >= 24:
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    table("choppy third", [r for r in rows if r["eff"] is not None and r["eff"] <= lo])
    table("middle third", [r for r in rows if r["eff"] is not None and lo < r["eff"] < hi])
    table("straight third", [r for r in rows if r["eff"] is not None and r["eff"] >= hi])
print("DONE")
