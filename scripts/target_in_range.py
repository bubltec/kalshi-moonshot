"""How Kalshi sets the target, and whether the target's place in the recent range says which side wins (2026-11-16, user: "i'm curious if we could re-create how they calculate the target"; "one thing i do visually is look at the distance the target is from the spot (cushion) and ... where the current target is in comparison with the hour or 15 min scope").

Every settled 15-minute market of the live crypto lane's series since Monday 2026-11-09, with the settlement index's ticks from the tape (variants.crypto_index). Time is strict: the range of the hour and of the 15 minutes before a market ends AT its open, the index is read in 5 s buckets stamped at their END (so a reading holds only ticks from before its stamp), and the buy is priced at the first Kalshi quote from minute 1.
A. The target: how far the recorded strike is from six guesses made from the index around the open (the last tick before it, and averages of the 15, 30 and 60 s before and of the 60 s after), in basis points.
B. The target's place in the range: where the strike sits in the index's range over the hour before the open and over the 15 minutes before it (0 = at the low, 1 = at the high). For each band: how often YES won, and what buying YES and buying NO at the ask at minute 1 returned held to settlement, fee in.
Bands fixed before the run. No coin is named.

Run on the host: scripts/cloud.sh run scripts/target_in_range.py
"""
import bisect
import datetime
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.strategy import fee_per_contract as fee

ET = datetime.timezone(datetime.timedelta(hours=-5))
SINCE = datetime.datetime(2026, 11, 9, 0, 0, tzinfo=ET).timestamp()
s = config.load()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
series = sorted(set(lane_specs(s)["crypto_multiscale"].series))
err = defaultdict(list)
rows = []  # (pos60, pos15, yes won, yes ask, no ask)
for ser in series:
    ix = variants.crypto_index(ser)
    if not ix:
        continue
    bt, bv, last = [], [], -1
    for r in tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, SINCE - 4000)).fetchall():
        b = int(float(r[0]) // 5)
        if b != last:
            bt.append((b + 1) * 5.0); bv.append(float(r[1])); last = b  # stamped at the END of its 5 s
        else:
            bv[-1] = float(r[1])
    for w in st.execute("SELECT ticker, close_ts, result, strike FROM windows WHERE series=? AND close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts", (ser, SINCE)).fetchall():
        close, strike = float(w["close_ts"]), float(w["strike"])
        opened = close - 900.0
        tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, opened - 65, opened + 65)).fetchall()
        tk = [(float(x[0]), float(x[1])) for x in tk]
        before = [v for t, v in tk if t <= opened]
        if len(before) >= 30:
            bp = lambda g: abs(g / strike - 1.0) * 1e4  # noqa: E731
            err["last tick before the open"].append(bp(before[-1]))
            for n in (15, 30, 60):
                xs = [v for t, v in tk if opened - n < t <= opened]
                if xs:
                    err[f"average of the {n} s before the open"].append(bp(sum(xs) / len(xs)))
            after = [v for t, v in tk if opened < t <= opened + 60]
            if after:
                err["average of the 60 s after the open"].append(bp(sum(after) / len(after)))
                err["first tick after the open"].append(bp(after[0]))
        j = bisect.bisect_right(bt, opened)  # buckets that ended at or before the open
        i60, i15 = bisect.bisect_right(bt, opened - 3600.0), bisect.bisect_right(bt, opened - 900.0)
        if j - i60 < 600:
            continue
        h, q15 = bv[i60:j], bv[i15:j]
        if max(h) <= min(h) or max(q15) <= min(q15):
            continue
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 1", (w["ticker"], opened + 60, opened + 120)).fetchone()
        if not q or not q[2] or float(q[2]) - float(q[1] or 0) > 0.04 or not (0.05 < float(q[2]) < 0.95):
            continue
        rows.append(((strike - min(h)) / (max(h) - min(h)), (strike - min(q15)) / (max(q15) - min(q15)), w["result"] == "yes", float(q[2]), 1.0 - float(q[1])))
    del bt, bv

print(f"window: 2026-11-09 00:00 ET on")
print("\nA. the recorded target against guesses from the index around the open (basis points off; 1 bp = 0.01%)")
for name, xs in sorted(err.items(), key=lambda kv: statistics.median(kv[1])):
    xs = sorted(xs)
    print(f"  {name:36s} n {len(xs):5d} | median {statistics.median(xs):5.2f} bp | 90% within {xs[int(0.9 * (len(xs) - 1))]:6.2f} bp | exact to 0.1 bp {sum(1 for x in xs if x < 0.1) / len(xs):4.0%}")


def table(title, k):
    print(f"\n{title}")
    for lo, hi, name in ((-99, 0.0, "under the range's low"), (0.0, 0.2, "bottom fifth"), (0.2, 0.4, "0.2 to 0.4"), (0.4, 0.6, "middle"), (0.6, 0.8, "0.6 to 0.8"), (0.8, 1.0001, "top fifth"), (1.0001, 99, "over the range's high")):
        rs = [r for r in rows if lo <= r[k] < hi]
        if len(rs) < 30:
            print(f"  {name:24s} n {len(rs):4d}")
            continue
        n = len(rs)
        yes = sum((1.0 if r[2] else 0.0) / (r[3] + fee(r[3])) - 1.0 for r in rs) / n * 100
        no = sum((0.0 if r[2] else 1.0) / (r[4] + fee(r[4])) - 1.0 for r in rs) / n * 100
        print(f"  {name:24s} n {n:4d} | YES won {sum(1 for r in rs if r[2]) / n:4.0%} | YES ask at minute 1 {sum(r[3] for r in rs) / n:.3f} | buy YES {yes:+6.1f}% | buy NO {no:+6.1f}%")


print(f"\nB. {len(rows)} markets with an hour of the index before the open and a quote at minute 1")
table("the target's place in the HOUR before the open", 0)
table("the target's place in the 15 MINUTES before the open", 1)
print("DONE")
