import math, bisect
from kalshi_moonshot import config, db
s = config.load()
tape = db.connect(s.tape_path, "tape", shared=True)
T0 = 1794692100.0  # 16:35:00 ET? computed below
import datetime
buy = datetime.datetime(2026, 11, 14, 21, 35, 45, tzinfo=datetime.timezone.utc).timestamp()
open_ = datetime.datetime(2026, 11, 14, 21, 30, 0, tzinfo=datetime.timezone.utc).timestamp()
ids = [r[0] for r in tape.execute("SELECT DISTINCT index_id FROM index_ticks WHERE ts>? AND ts<=? AND (index_id LIKE ? OR index_id LIKE ?)", (buy - 600, buy, "%BTC%", "%btc%"))]
print("IDS", ids)
STRIKE = 64524.70
for ix in ids:
    rows = [(float(t), float(v)) for t, v in tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (ix, buy - 3700, buy + 560))]
    if len(rows) < 100: 
        print("ROW", ix, "few", len(rows)); continue
    ts = [r[0] for r in rows]
    def at(t):
        i = bisect.bisect_right(ts, t) - 1
        return rows[i][1] if i >= 0 else None
    lv = [at(buy - 60 * k) for k in range(61)]
    mv = [math.log(lv[k] / lv[k + 1]) for k in range(60) if lv[k] and lv[k + 1]]
    rms = math.sqrt(sum(m * m for m in mv) / len(mv))
    left = 554.0
    sig = rms * math.sqrt(left / 60.0)
    spot = at(buy)
    cush = math.log(STRIKE / spot)
    win = [(t, v) for t, v in rows if open_ <= t <= buy]
    lo = min(win, key=lambda r: r[1]); hi = max(win, key=lambda r: r[1])
    print("IDX", ix, f"n={len(rows)} spot_at_buy={spot:.1f} under_strike={STRIKE - spot:+.1f} ({cush*1e4:.1f}bp) typical_minute={rms*1e4:.1f}bp sigma_left={sig*1e4:.1f}bp cushion_in_sigma={cush/sig:.2f}")
    print("   ", "window low %.1f at +%ds, high %.1f at +%ds; spot %ds/60s/120s/180s/300s ago: %s" % (lo[1], lo[0]-open_, hi[1], hi[0]-open_, 0, " ".join(f"{at(buy-k):.0f}" for k in (60,120,180,300))))
    print("   ", "hour ago %.0f, 30 min ago %.0f, 15 min ago %.0f; after: +60s %.0f +180s %.0f +300s %.0f" % (at(buy-3600), at(buy-1800), at(buy-900), at(buy+60), at(buy+180), at(buy+300)))
print("DONE")
