"""Do bets against a run of same-direction windows lose more? (2026-11-11, user, with Kalshi's list of the natgas windows' settled values: "Are we even looking at this too?")
The natgas 4 PM bet was a NO (Down) after six windows in a row that settled Up and a settlement level that rose from 3.48391 to 3.51319 in 75 minutes. We use Kalshi's settled values only to learn the offset between our feed and Kalshi's number
(commodities.py); no rule reads the run of results. Question, on every settled live bet since SINCE (default 2026-11-09, the Monday rewrite), crypto and commodity lanes:
for the windows of the same series just before the bet's window (consecutive 15-minute windows only), how long is the run of one result, and did the bet go with it or against it? Buckets: against a run of 3 or more, against a run of 1-2,
with a run of 1-2, with a run of 3 or more, and no clean history. As it happened, held to settlement, total losses, halves. The windows' results come from the windows table (strike and result; the settled value itself is not stored).
    scripts/cloud.sh run scripts/streak_check.py        (or: rpy.sh)
"""
import datetime as dt, os, re, time
from kalshi_moonshot import config
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload(); st = Store(s.db_path, s.exclude_versions)
ET = dt.timezone(dt.timedelta(hours=-5)); MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
SINCE = dt.datetime.fromisoformat(os.environ.get("SINCE", "2026-11-09")).replace(tzinfo=ET).timestamp()
def close_of(tk):
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", tk)
    return dt.datetime(2000 + int(m[1]), MON[m[2]], int(m[3]), int(m[4]), int(m[5]), tzinfo=ET).timestamp()
rows = st.db.execute("SELECT ts, lane, ticker, side, ask, stake_usd, outcome, exit_price, exit_reason FROM decisions WHERE action='bet' AND mode='trade' AND lane IN ('crypto_multiscale','commodity_multiscale') AND outcome IN ('yes','no') AND stake_usd>0 AND ask>0 AND ts>=? ORDER BY ts", (SINCE,)).fetchall()
bets = []
for r in rows:
    tk, ser = r["ticker"], r["ticker"].split("-")[0]
    c = close_of(tk)
    prev = st.db.execute("SELECT close_ts, result FROM windows WHERE series=? AND close_ts<? AND result IN ('yes','no') ORDER BY close_ts DESC LIMIT 8", (ser, c - 60)).fetchall()
    run, direction, expect = 0, None, c - 900
    for p in prev:
        if abs(float(p["close_ts"]) - expect) > 120: break
        if direction is None: direction = p["result"]
        if p["result"] != direction: break
        run += 1; expect -= 900
    ask, stake = float(r["ask"]), float(r["stake_usd"]); cost = ask + fee(ask)
    won = 1.0 if r["outcome"] == r["side"] else 0.0
    held = stake * (won / cost - 1.0)
    if r["exit_price"] is not None:
        px = float(r["exit_price"]); back = px if str(r["exit_reason"] or "").startswith("curve exit (resting ask)") else px - EXIT_SLIP - fee(px); real = stake * (back / cost - 1.0)
    else: real = held
    kind = "no clean history" if not run else (("with" if r["side"] == direction else "against") + (" a run of 3+" if run >= 3 else " a run of 1-2"))
    bets.append(dict(kind=kind, ts=float(r["ts"]), stake=stake, real=real, held=held, tl=(r["exit_price"] is None and won == 0.0), lane=r["lane"], run=run))
print(f"{len(bets)} settled live bets since {os.environ.get('SINCE', '2026-11-09')}")
def line(label, xs):
    if not xs: return f"  {label:28s} n=0"
    n = len(xs); stk = sum(x["stake"] for x in xs); h = sorted(xs, key=lambda x: x["ts"]); a, b = h[: n // 2], h[n // 2:]
    return f"  {label:28s} n={n:3d} | as happened {sum(x['real'] for x in xs):+7.2f} ({sum(x['real'] for x in xs) / stk:+6.1%}) | held {sum(x['held'] for x in xs):+7.2f} | total losses {sum(1 for x in xs if x['tl']):2d} (${sum(x['stake'] for x in xs if x['tl']):5.2f}) | halves {sum(x['real'] for x in a):+.2f} / {sum(x['real'] for x in b):+.2f}"
for lane in (None, "crypto_multiscale", "commodity_multiscale"):
    sub = [x for x in bets if lane is None or x["lane"] == lane]
    print(lane or "both lanes")
    for k in ("against a run of 3+", "against a run of 1-2", "with a run of 1-2", "with a run of 3+", "no clean history"):
        print(line(k, [x for x in sub if x["kind"] == k]))
print("DONE")
