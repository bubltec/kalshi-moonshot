"""Will the new price band change how often the live lanes can bet? (2026-11-11, user: "is this going to impact how often it bets?")
The live lanes moved from a price band of 0.55-0.75 (crypto) / 0.55-0.80 (commodity) to 0.70-0.90. Over the last HOURS (default 6), for each lane's own entry minutes (crypto 1.5-14, commodity 4-14 of the 15), one quote a second
per market: the share of market-seconds where a side's ask (YES = the ask; NO = 1 - the bid) was inside the old band, inside the new band, inside both, and in neither; and the share of markets (windows) that had a side in each band at
least once in the entry minutes. A market-second in band is only the first filter: the curve model, the curve-edge gate and the price rules come after it. Read-only.
    scripts/cloud.sh run scripts/band_change_check.py [HOURS=6]
"""
import collections, datetime as dt, os, re, time
from kalshi_moonshot import config, db
s = config.reload(); tape = db.connect(s.tape_path, "tape", shared=True)
ET = dt.timezone(dt.timedelta(hours=-5)); MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
HOURS = float(os.environ.get("HOURS", "6")); now = time.time()
LANES = {"crypto": (["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M"], 1.5, 14.0, (0.55, 0.75)),
         "commodity": (["KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXBRENT15M", "KXNATGAS15M", "KXCOPPER15M", "KXPLATINUM15M", "KXPALLADIUM15M"], 4.0, 14.0, (0.55, 0.80))}
NEW = (0.70, 0.90)
def close_of(tk):
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", tk)
    return dt.datetime(2000 + int(m[1]), MON[m[2]], int(m[3]), int(m[4]), int(m[5]), tzinfo=ET).timestamp()
print(f"last {HOURS:g} h; share of market-seconds in the lane's entry minutes, and of windows with a side in the band at least once")
for name, (series, lo, hi, old) in LANES.items():
    like = " OR ".join("ticker LIKE ?" for _ in series)
    rows = tape.execute(f"SELECT ts,ticker,bid,ask FROM quotes WHERE ts>? AND ({like}) AND bid>0 AND ask>0 ORDER BY ts", (now - HOURS * 3600, *[x + "-%" for x in series])).fetchall()
    seen, n = set(), collections.Counter(); win = collections.defaultdict(lambda: [0, 0, 0])
    for ts, tk, bid, ask in rows:
        c = close_of(tk); el = 15.0 - (c - ts) / 60.0
        if not (lo <= el <= hi) or (tk, int(ts)) in seen: continue
        seen.add((tk, int(ts)))
        sides = (float(ask), 1.0 - float(bid))
        o = any(old[0] <= a <= old[1] for a in sides); w = any(NEW[0] <= a <= NEW[1] for a in sides)
        n["all"] += 1; n["old"] += o; n["new"] += w; n["both"] += (o and w); n["neither"] += (not o and not w); n["new only"] += (w and not o); n["old only"] += (o and not w)
        r = win[tk]; r[0] += 1; r[1] += o; r[2] += w
    tot = n["all"]
    print(f"\n{name}: old band {old[0]:.2f}-{old[1]:.2f}, new band {NEW[0]:.2f}-{NEW[1]:.2f}; {tot} market-seconds, {len(win)} windows")
    for k in ("old", "new", "both", "old only", "new only", "neither"):
        print(f"   in {k:9s}: {n[k] / tot:6.1%}")
    print(f"   windows with a side in the old band at least once: {sum(1 for r in win.values() if r[1]) / len(win):.0%}; in the new band: {sum(1 for r in win.values() if r[2]) / len(win):.0%}")
print("DONE")
