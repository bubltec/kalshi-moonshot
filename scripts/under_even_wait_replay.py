"""What wait should the under-even sale have? (2026-11-17, user, after its first live sale sold KXCOPPER15M-26NOV171130 NO at 0.33 with 213 s left and the bet won on a move in the last 15 s: "the under 50c
sale needs a wait, and it needs to scale so a blip for a couple minutes doesn't cause it to crash out. we would only sell if it doesn't stop losing value"; "copper has always been kind of volatile. it would
probably benefit from a kalshi perps spot tracker"; "all the volatile commodities would. oil, gas, brent".)

One question on the bot's own bets (CLAUDE.md rule 4): every live commodity bet in the window (SINCE/UNTIL=YYYY-MM-DDTHH:MM, default since the risk exit went live, 2026-11-13 11:02 ET), followed second by
second on Kalshi's own quote from the buy to the moment it really ended, fees on every leg, against what actually happened. A sale decided at second t fills at the worse of the bids at t+1 and t+2. Six
forms of the sale, built only from the three things the user named, each stated before the run:
  A  as it went live: the bid under even (0.50) without a break for a tenth of the time left
  B  a longer wait: under even for a quarter of the time left
  C  B, and still losing value: the lowest bid of the second half of that stretch is under the lowest of its first half (it is still making new lows)
  D  A, and the Kalshi perp agrees: the long layer's chance (curverisk.long_chance: the perp's distance from its value at the open over what it typically travels in the time left) is under the lanes' own
     bar (curverisk.confirm_bar(0.2, left): 0.2, rising to 1 across the last 180 s). This is the hold the lanes' other loss sales already have; the live under-even sale skipped it.
  E  B with the perp, F  C with the perp.
With no perp reading (under an hour of ticks, or stale) D, E and F do not sell. Bets bought under even are left out, as live. The split by shape: how far under even the bid was when the stretch began
to count as long enough (a slide or a cliff) is not used; the rows are by the price paid, which decides what a sale can save. No commodity is named in a rule.
    scripts/cloud.sh run scripts/under_even_wait_replay.py [SINCE=...] [UNTIL=...] [LIST=1]
"""
import bisect
import datetime
import os
import sys
import traceback

from kalshi_moonshot import config, curverisk, db, perpref
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
rows = st.execute("SELECT ts, ticker, lane, side, ask, stake_usd, contracts, exit_price, exit_ts, exit_reason FROM decisions WHERE lane LIKE 'commodity%' AND mode='trade' AND action='bet' AND stake_usd>0 AND contracts>0 AND ts>? AND ts<? ORDER BY ts", (SINCE, UNTIL)).fetchall()
RULES = {"A": (0.10, False, False), "B": (0.25, False, False), "C": (0.25, True, False), "D": (0.10, False, True), "E": (0.25, False, True), "F": (0.25, True, True)}
NAMES = {"A": "A  a tenth of the time left (as it went live)", "B": "B  a quarter of the time left", "C": "C  a quarter, and still making new lows", "D": "D  a tenth, and the Kalshi perp agrees", "E": "E  a quarter, and the perp agrees",
         "F": "F  a quarter, new lows, and the perp agrees"}
out, noperp = [], 0
for r in rows:
    if r["ticker"] not in results:
        continue
    res, close = results[r["ticker"]]
    t0, side, opened = float(r["ts"]), r["side"], close - 900.0
    stake, n = float(r["stake_usd"]), float(r["contracts"])
    basis = stake / n
    if basis < curverisk.EVEN:
        continue
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 30, close)).fetchall()
    if len(q) < 10:
        continue
    qt = [float(x[0]) for x in q]
    qb = [float(x[1]) if side == "yes" else 1.0 - float(x[2]) for x in q]
    won = res == side
    sold = r["exit_price"] is not None and r["exit_ts"] is not None and float(r["exit_ts"]) < close - 1
    if sold and "(under even)" in (r["exit_reason"] or ""):
        sold = False  # the live sale under study: this bet is followed to the close and scored against holding, so each form shows what it would have done
    actual = ((float(r["exit_price"]) - fee(float(r["exit_price"])) if sold else (1.0 if won else 0.0)) - basis) * n
    end = float(r["exit_ts"]) if sold else close
    path = []
    for t in range(int(t0) + 1, int(end) - 1):
        i = bisect.bisect_right(qt, t) - 1
        if i >= 0 and t - qt[i] <= 60:
            path.append((t, qb[i]))
    if not path:
        continue
    kp = perpref.index_for_series(r["ticker"].split("-")[0])
    pk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (kp, opened - 3800, close)).fetchall() if kp else []
    PT, PV = [float(x[0]) for x in pk], [float(x[1]) for x in pk]
    j = bisect.bisect_left(PT, opened)
    ref = PV[j] if j < len(PT) and PT[j] <= opened + 60.0 else 0.0
    if ref <= 0:
        noperp += 1
    memo = {}

    def perp_ok(t, left, PT=PT, PV=PV, ref=ref, side=side, memo=memo):
        """True when the long layer does not hold the sale: its chance is under the faded bar. No reading = hold."""
        if t not in memo:
            k = bisect.bisect_right(PT, t)
            c = curverisk.long_chance(PT[:k], PV[:k], float(t), ref, side, left) if ref > 0 else None
            memo[t] = (c, c is not None and c < curverisk.confirm_bar(0.2, left))
        return memo[t][1]

    sale, since = {}, None
    for k, (t, bid) in enumerate(path):
        left = close - t
        if left <= 8:
            break
        if bid >= curverisk.EVEN:
            since = None
            continue
        if since is None:
            since = k
        for name, (share, lows, perp) in RULES.items():
            if name in sale or bid < 0.05 or t - path[since][0] < share * left:
                continue
            if lows:
                seg = [b for _, b in path[since : k + 1]]
                half = len(seg) // 2
                if half < 1 or min(seg[half:]) >= min(seg[:half]) - 1e-9:
                    continue
            if perp and not perp_ok(t, left):
                continue
            got = [b for _, b in path[k + 1 : k + 3]]
            f = min(got) if got else bid
            sale[name] = ((f - fee(f) - basis) * n, f, left, memo.get(t, (None,))[0])
        if len(sale) == len(RULES):
            break
    out.append({"r": r, "close": close, "t0": t0, "basis": basis, "won": won, "actual": actual, "sale": sale, "stake": stake})


def line(name, rs, rule):
    if not rs:
        print(f"    {name:22s} n   0")
        return
    a = sum(x["actual"] for x in rs)
    w = sum(x["sale"][rule][0] if rule in x["sale"] else x["actual"] for x in rs)
    tk = [x for x in rs if rule in x["sale"]]
    wins = [x for x in tk if x["won"]]
    print(f"    {name:22s} n {len(rs):3d} | what happened ${a:+8.2f} | with the rule ${w:+8.2f} | change ${w - a:+7.2f} | sold {len(tk):3d}: beat {sum(1 for x in tk if x['sale'][rule][0] > x['actual'] + 1e-9):3d}, lost to {sum(1 for x in tk if x['sale'][rule][0] < x['actual'] - 1e-9):3d}"
          f" | winners sold {len(wins):2d}" + (f" | median sale {sorted(x['sale'][rule][1] for x in tk)[len(tk) // 2]:.2f} with {sorted(x['sale'][rule][2] for x in tk)[len(tk) // 2]:.0f}s left" if tk else ""))


print(f"window: live commodity bets {datetime.datetime.fromtimestamp(SINCE, ET):%m-%d %H:%M} to {datetime.datetime.fromtimestamp(min(UNTIL, datetime.datetime.now().timestamp()), ET):%m-%d %H:%M} ET; {len(out)} settled, bought at or over even; {noperp} without a perp reference at the open")
for rule in (RULES if not os.environ.get("BRIEF") else ()):
    print(f"\n  {NAMES[rule]}")
    line("all", out, rule)
    line("paid under 0.80", [x for x in out if x["basis"] < 0.80], rule)
    line("paid 0.80 and over", [x for x in out if x["basis"] >= 0.80], rule)
if os.environ.get("LIST"):
    print("\n  bet by bet (only bets some form sells)")
    for x in out:
        if x["sale"] and (os.environ["LIST"] == "1" or os.environ["LIST"] in x["r"]["ticker"]):
            r = x["r"]
            print(f"    {datetime.datetime.fromtimestamp(x['t0'], ET):%m-%d %H:%M} {r['ticker'][:26]:26s} paid {x['basis']:.2f} actual ${x['actual']:+6.2f} ({'won' if x['won'] else 'lost'}) | " + " | ".join(f"{k} {v[1]:.2f}@{v[2]:.0f}s ${v[0]:+.2f}" + (f" perp {v[3]:.2f}" if v[3] is not None else "") for k, v in sorted(x["sale"].items())))
print("DONE")
