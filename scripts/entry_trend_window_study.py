"""The entry trend check's window, replayed on the live lane's buys (2026-11-14 night, user: "Yes, design it", after the buys study found that buys made with no recent move toward the side lost: the lowest third by the spot's 3-minute move, 143 buys, -$67).
The check exists already: runner.side_trend_z, lane fields entry_trend_seconds (live 60) and entry_trend_z (live 0.0): buy only when the side's price has gained over the last entry_trend_seconds, in the market's own units (the move over that span divided by the standard deviation of that span's changes over the previous 2 minutes). Candidates move only how that existing check reads: the span (60, 120, 180, 240 s) with the noise taken over the previous max(120 s, 2 x span), and the threshold z (0.0 live).
Same buys: every live crypto_multiscale buy since Mon 2026-11-09 00:00 ET. For each variant: the buys it would have kept and the buys it would have dropped, n, net dollars and net per $1 staked (real exit or settlement, fee on both legs). A variant that drops buys that lost helps; the buys before today's check (set today ~10:00) were bought without it, so the gate is replayed on all of them. Said plainly: spans and thresholds chosen after seeing the buys study.
Run on the host: python scripts/entry_trend_window_study.py"""
import bisect
import datetime
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 5, 0, tzinfo=datetime.UTC).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.execute("SELECT ts, ticker, side, contracts, stake_usd, exit_price, outcome FROM decisions WHERE lane='crypto_multiscale' AND mode='trade' AND action='bet' AND stake_usd>0 AND ts>? AND ticker LIKE '%15M-%' ORDER BY ts", (SINCE,)).fetchall()


def z_of(ts, mid, t, span):
    at = lambda x: mid[max(bisect.bisect_right(ts, x) - 1, 0)]
    cur, then = at(t), at(t - span)
    window = max(120.0, 2.0 * span)
    n = int(window - span)
    grid = [at(t - 5.0 - k) for k in range(int(window))]
    steps = [a - b for a, b in zip(grid[:n], grid[int(span):int(span) + n])]
    if len(steps) < 20 or ts[0] > t - span - 5.0:
        return None
    sd = statistics.pstdev(steps)
    return (cur - then) / sd if sd > 0 else (0.0 if cur == then else float("inf") * (1 if cur > then else -1))


buys = []
for r in rows:
    t = float(r["ts"]); side = r["side"]; n, cost = float(r["contracts"]), float(r["stake_usd"])
    if r["exit_price"] is not None:
        v = n * (float(r["exit_price"]) - fee_per_contract(float(r["exit_price"]))) - cost
    elif r["outcome"] in ("yes", "no"):
        v = (n if r["outcome"] == side else 0.0) - cost
    else:
        continue
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? ORDER BY ts", (r["ticker"], t - 900.0, t)).fetchall()
    if len(q) < 30:
        continue
    ts = [float(x[0]) for x in q]; mid = [((float(x[1]) + float(x[2])) / 2.0 if side == "yes" else 1.0 - (float(x[1]) + float(x[2])) / 2.0) for x in q]
    buys.append(dict(t=t, v=v, cost=cost, z={sp: z_of(ts, mid, t, sp) for sp in (60.0, 120.0, 180.0, 240.0)}))
print(f"live crypto_multiscale buys since Mon 2026-11-09 with quotes and a result: {len(buys)}; net {sum(b['v'] for b in buys):+.2f}")


def line(name, grp):
    if not grp:
        return f"   {name:28s} n=  0"
    return f"   {name:28s} n={len(grp):3d} net {sum(b['v'] for b in grp):+8.2f} per $1 {sum(b['v'] for b in grp) / sum(b['cost'] for b in grp):+.3f} lost {sum(1 for b in grp if b['v'] < 0)}"


for span in (60.0, 120.0, 180.0, 240.0):
    for thr in (0.0, 0.5):
        kept = [b for b in buys if b["z"][span] is not None and b["z"][span] > thr]
        dropped = [b for b in buys if b["z"][span] is None or b["z"][span] <= thr]
        tag = " (live)" if span == 60.0 and thr == 0.0 else ""
        print(f"\nspan {span:.0f} s, needs z > {thr}{tag}")
        print(line("kept", kept)); print(line("dropped", dropped))
both = [b for b in buys if all(b["z"][sp] is not None and b["z"][sp] > 0.0 for sp in (60.0, 180.0))]
dropb = [b for b in buys if b not in both]
print("\nboth the 60 s and the 180 s reading above 0")
print(line("kept", both)); print(line("dropped", dropb))
today = [b for b in buys if b["t"] >= datetime.datetime(2026, 11, 14, 10, 0, tzinfo=ET).timestamp()]
print(f"\nbuys since 10:00 today (the live check was on): {len(today)}; of them z60 <= 0 (should be none): {sum(1 for b in today if b['z'][60.0] is not None and b['z'][60.0] <= 0)}")
print("DONE")
