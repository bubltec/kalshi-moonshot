"""How often does the climb_down lane's rule, exactly as built, find an entry on the recorded quotes? Read-only.

The lane (data/strategies.toml [lane.climb_down]) buys the NO side of a coin's 15-minute market when, on a streamed quote:
    - the NO ask is between PRICE_LO and PRICE_HI (0.40-0.43) and the NO spread is at most MAX_SPREAD (0.03),
    - the window is between ELAPSED_LO and ELAPSED_HI minutes old (0.5-14),
    - the NO mid is at least TREND (0.25) above where it was at the latest quote 60-120 seconds earlier (no quote that far back means no buy; entry_trend_seconds = 60).
The first quote of a market that meets all of that is an entry. This counts them, and counts the near misses, so a lane with no bets can be told from a lane that is
simply waiting:
    entries          markets with a quote meeting every condition
    near trend       in the band, but the rise was TREND_NEAR (0.15) to TREND: the filter was close
    jumped over      a rise of at least TREND whose NO ask was above the band (up to 0.60) when seen: the climb skipped past 40-43c between quotes
    in band, flat    markets whose NO ask was in the band at some point at all (the entry watcher woke up), whatever the trend

    python - [SINCE_HOURS=24] [SERIES=KXBTC15M,KXETH15M,KXSOL15M,KXXRP15M,KXDOGE15M] < scripts/climb_rule_replay.py          (inside the bot container)
    scripts/cloud.sh run scripts/climb_rule_replay.py SINCE_HOURS=72
"""
import bisect
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

SINCE_HOURS = float(os.environ.get("SINCE_HOURS", "24"))
SERIES = tuple(os.environ.get("SERIES", "KXBTC15M,KXETH15M,KXSOL15M,KXXRP15M,KXDOGE15M").split(","))
PRICE_LO, PRICE_HI, MAX_SPREAD = 0.40, 0.43, 0.03
ELAPSED_LO, ELAPSED_HI = 0.5, 14.0
TREND, TREND_NEAR = 0.25, 0.15

since = time.time() - SINCE_HOURS * 3600
first = tape.execute("SELECT MIN(ts) FROM quotes").fetchone()[0]
since = max(since, float(first or since))
wins = main.db.execute(
    "SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? AND series IN (%s) ORDER BY close_ts" % ",".join("?" * len(SERIES)),
    (since + 900, *SERIES)).fetchall()

counts = {"markets": 0, "in band": 0, "entries": 0, "near trend": 0, "jumped over": 0}
entries = []
for w in wins:
    close = float(w["close_ts"])
    rows = [(float(t), float(b), float(a)) for t, b, a in tape.execute(
        "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 905, close)).fetchall()]
    if len(rows) < 30:
        continue
    counts["markets"] += 1
    ts = [r[0] for r in rows]
    entered = near = jumped = band = False
    for i, (t, yb, ya) in enumerate(rows):
        elapsed = (t - (close - 900)) / 60.0
        if not ELAPSED_LO <= elapsed <= ELAPSED_HI:
            continue
        no_ask, no_bid = 1.0 - yb, 1.0 - ya
        no_mid = (no_ask + no_bid) / 2.0
        j = bisect.bisect_right(ts, t - 60) - 1  # the latest quote at least 60 s back ...
        if j < 0 or ts[j] < t - 120:  # ... and no more than 120 s back
            continue
        rise = no_mid - ((1.0 - rows[j][1]) + (1.0 - rows[j][2])) / 2.0
        in_band = PRICE_LO <= no_ask <= PRICE_HI and (no_ask - no_bid) <= MAX_SPREAD + 1e-9
        band = band or (PRICE_LO <= no_ask <= PRICE_HI)
        if in_band and rise >= TREND and not entered:
            entered = True
            entries.append((t, w["series"], no_ask, rise, w["result"]))
        elif in_band and TREND_NEAR <= rise < TREND:
            near = True
        elif not in_band and rise >= TREND and PRICE_HI < no_ask <= 0.60:
            jumped = True
    counts["in band"] += band
    counts["entries"] += entered
    counts["near trend"] += (near and not entered)
    counts["jumped over"] += (jumped and not entered)

span_h = (time.time() - since) / 3600.0
print(f"{counts['markets']} coin markets on the tape over {span_h:.1f} hours (from {time.strftime('%m-%d %H:%M', time.gmtime(since))} UTC)")
print(f"  in band at some point: {counts['in band']}   entries: {counts['entries']}   near trend (15-25c): {counts['near trend']}   jumped over the band: {counts['jumped over']}")
if entries:
    print(f"  entries per day at this rate: {counts['entries'] / span_h * 24:.1f}")
    won = sum(1 for e in entries if e[4] == "no")
    print(f"  the NO side then won {won} of {len(entries)}; paid about {sum(e[2] for e in entries) / len(entries):.2f} on average")
    for e in entries[-6:]:
        print(f"    {time.strftime('%m-%d %H:%M:%S', time.gmtime(e[0]))} {e[1]:<10} NO ask {e[2]:.2f} after +{e[3] * 100:.0f}c   result {e[4]}")
