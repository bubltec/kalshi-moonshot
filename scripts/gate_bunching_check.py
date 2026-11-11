"""Do the live lanes buy the moment the time gate opens? (2026-11-11, user: "when there's a time gate it just buys as soon as its open and picks the side the target is on. it almost never waits to see what the curve is doing ... all the recent losses look this way.")
Over every settled live bet since SINCE (default 2026-11-09), for crypto_multiscale and commodity_multiscale: seconds between the lane's lower entry bound and the bet. The bound each lane had at the bet's time (from the lane history in the commit log and the user's changes):
  crypto_multiscale   1.5 min until 2026-11-11 about 18:00 ET, then 7.0;      commodity_multiscale  1.5 until 2026-11-11 07:15 ET, then 4.0 until about 18:00, then 7.0.
The change times are approximate (the change moments are not logged), so a bet within a few minutes of one is marked and left out of the first table. Then: the share of bets in the first 30, 60 and 120 s after the bound against the share a flat spread over the 15-minute window would give;
how the bets in the first 60 s did against the rest (as it happened, held to settlement, total losses); the share of bets on the side the spot already stood on against the target at the buy (the favorite side; YES when the contract's ask is the
higher priced, i.e. ask >= 0.5, which the 0.55-0.90 bands guarantee), and the last ten losing bets with their seconds after the bound.
    scripts/cloud.sh run scripts/gate_bunching_check.py        (or rpy.sh)
"""
import datetime as dt, os, re
from kalshi_moonshot import config
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload(); st = Store(s.db_path, s.exclude_versions)
ET = dt.timezone(dt.timedelta(hours=-5)); MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
day = dt.datetime(2026, 11, 11, tzinfo=ET); at = lambda h, m: day.replace(hour=h, minute=m).timestamp()
SINCE = dt.datetime.fromisoformat(os.environ.get("SINCE", "2026-11-09")).replace(tzinfo=ET).timestamp()
def close_of(tk):
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", tk)
    return dt.datetime(2000 + int(m[1]), MON[m[2]], int(m[3]), int(m[4]), int(m[5]), tzinfo=ET).timestamp()
def bound(lane, t):
    """(lower entry bound in minutes, near a change?)"""
    changes = {"crypto_multiscale": [(at(18, 0), 1.5, 7.0)], "commodity_multiscale": [(at(7, 15), 1.5, 4.0), (at(18, 0), 4.0, 7.0)]}[lane]
    lo = changes[0][1]
    near = False
    for when, before, after in changes:
        near = near or abs(t - when) < 600
        lo = after if t >= when else lo if t < when and lo == before else lo
    return lo, near
def lower(lane, t):
    ch = {"crypto_multiscale": [(at(18, 0), 7.0)], "commodity_multiscale": [(at(7, 15), 4.0), (at(18, 0), 7.0)]}[lane]
    lo = 1.5
    for when, v in ch:
        if t >= when: lo = v
    return lo
rows = st.db.execute("SELECT ts, lane, ticker, side, ask, stake_usd, outcome, exit_price, exit_reason FROM decisions WHERE action='bet' AND mode='trade' AND lane IN ('crypto_multiscale','commodity_multiscale') AND outcome IN ('yes','no') AND stake_usd>0 AND ask>0 AND ts>=? ORDER BY ts", (SINCE,)).fetchall()
bets = []
for r in rows:
    c = close_of(r["ticker"]); el = 15.0 - (c - float(r["ts"])) / 60.0
    lo = lower(r["lane"], float(r["ts"]))
    near = any(abs(float(r["ts"]) - w) < 600 for w in ((at(18, 0),) if r["lane"] == "crypto_multiscale" else (at(7, 15), at(18, 0))))
    ask, stake = float(r["ask"]), float(r["stake_usd"]); cost = ask + fee(ask)
    won = 1.0 if r["outcome"] == r["side"] else 0.0; held = stake * (won / cost - 1.0)
    if r["exit_price"] is not None:
        px = float(r["exit_price"]); back = px if str(r["exit_reason"] or "").startswith("curve exit (resting ask)") else px - EXIT_SLIP - fee(px); real = stake * (back / cost - 1.0)
    else: real = held
    bets.append(dict(lane=r["lane"], ts=float(r["ts"]), after=(el - lo) * 60.0, el=el, lo=lo, near=near, real=real, held=held, tl=(r["exit_price"] is None and won == 0.0), tk=r["ticker"].split("-")[0], side=r["side"], ask=ask))
use = [b for b in bets if not b["near"] and b["after"] >= -30]
print(f"{len(bets)} settled live bets since {os.environ.get('SINCE', '2026-11-09')}; {len(use)} used (bets within 10 minutes of a gate change are left out)")
def line(label, xs):
    if not xs: return f"  {label:34s} n=0"
    st_ = sum(1 for _ in xs); stake = sum(1 for _ in xs)
    return f"  {label:34s} n={len(xs):3d} | as happened {sum(x['real'] for x in xs):+7.2f} | held {sum(x['held'] for x in xs):+7.2f} | total losses {sum(1 for x in xs if x['tl']):2d}"
for lane in ("crypto_multiscale", "commodity_multiscale"):
    xs = [b for b in use if b["lane"] == lane]
    if not xs: continue
    width = (15.0 - xs[0]["lo"]) * 60.0
    print(f"\n{lane}: {len(xs)} bets; the entry window (at the bound each had) is 9-13 minutes wide; seconds after the lower bound:")
    for cut in (30, 60, 120, 300):
        n = sum(1 for b in xs if b["after"] <= cut)
        flat = sum(min(1.0, cut / ((15.0 - b["lo"]) * 60.0)) for b in xs) / len(xs)
        print(f"   within {cut:3d} s of the bound: {n / len(xs):5.0%} of bets (a flat spread over each bet's window would give {flat:4.0%})")
    print(line("first 60 s after the bound", [b for b in xs if b["after"] <= 60]))
    print(line("later than 60 s", [b for b in xs if b["after"] > 60]))
allb = [b for b in use]
print("\nboth lanes")
print(line("first 60 s after the bound", [b for b in allb if b["after"] <= 60]))
print(line("60 s to 5 min after", [b for b in allb if 60 < b["after"] <= 300]))
print(line("later than 5 min", [b for b in allb if b["after"] > 300]))
fav = sum(1 for b in use if b["ask"] >= 0.5)
print(f"\nbets on the favorite side (ask >= 0.5): {fav} of {len(use)}")
print("\nthe last ten losing bets (as it happened under -50% of stake), seconds after the lane's lower bound:")
loss = [b for b in bets if b["real"] < -0.5 * 1 and b["tl"] or b["real"] < -1.0]
for b in sorted(bets, key=lambda b: b["ts"], reverse=True):
    if b["real"] < 0 and (b["tl"] or b["real"] < -1.0):
        print(f"   {dt.datetime.fromtimestamp(b['ts'], ET).strftime('%m-%d %H:%M')} {b['lane'][:6]} {b['tk']:14s} {b['side']} {b['ask']:.2f} {b['real']:+6.2f}  {b['after']:+6.0f} s after the bound (minute {b['el']:.1f}){'  (near a gate change)' if b['near'] else ''}")
        if sum(1 for _ in range(1)) and (loss := loss): pass
        if not hasattr(line, "n"): line.n = 0
        line.n += 1
        if line.n >= 10: break
print("DONE")
