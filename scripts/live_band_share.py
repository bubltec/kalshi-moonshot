"""Were the markets inside the live lanes' price bands? (2026-11-11, user: "do you know if the price moved less than 55 or higher than 80?")
Per ET hour, over the live lanes' entry minutes (crypto 1.5-14, commodity 4-14 of the 15), one quote a second per market: the share of market-seconds where a side's ask was inside the lane's band (crypto 0.55-0.75, commodity 0.55-0.80), where BOTH sides asked under 0.55 (a coin flip), or where the favorite's ask was over the band's top. A side's ask: YES = the quote's ask, NO = 1 - the quote's bid. Read-only."""
import collections, datetime as dt, re, time
from kalshi_moonshot import config, db
s = config.reload(); tape = db.connect(s.tape_path, "tape", shared=True)
ET = dt.timezone(dt.timedelta(hours=-5))
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
now = time.time()
LANES = {"crypto": (["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M"], 1.5, 14.0, 0.55, 0.75),
         "commodity": (["KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXBRENT15M", "KXNATGAS15M", "KXCOPPER15M", "KXPLATINUM15M", "KXPALLADIUM15M"], 4.0, 14.0, 0.55, 0.80)}
def close_ts(tk):
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", tk)
    if not m: return None
    y, mo, d, hh, mm = 2000 + int(m[1]), MON[m[2]], int(m[3]), int(m[4]), int(m[5])
    return dt.datetime(y, mo, d, hh, mm, tzinfo=ET).timestamp()
for name, (series, lo, hi, band_lo, band_hi) in LANES.items():
    start = now - 8 * 3600
    like = " OR ".join("ticker LIKE ?" for _ in series)
    rows = tape.execute(f"SELECT ts,ticker,bid,ask FROM quotes WHERE ts>? AND ({like}) AND bid>0 AND ask>0 ORDER BY ts", (start, *[x + "-%" for x in series])).fetchall()
    last = {}
    by = collections.defaultdict(lambda: [0, 0, 0, 0])  # hour -> [seconds, in band, both under, favorite over]
    for ts, tk, bid, ask in rows:
        c = close_ts(tk)
        if c is None: continue
        el = 15.0 - (c - ts) / 60.0
        if not (lo <= el <= hi): continue
        k = (tk, int(ts))
        if k in last: continue
        last[k] = 1
        ya, na = float(ask), 1.0 - float(bid)
        h = dt.datetime.fromtimestamp(ts, ET).strftime("%H")
        r = by[h]; r[0] += 1
        if band_lo <= ya <= band_hi or band_lo <= na <= band_hi: r[1] += 1
        elif max(ya, na) < band_lo: r[2] += 1
        elif max(ya, na) > band_hi: r[3] += 1
    print(f"\n{name}: band {band_lo:.2f}-{band_hi:.2f}, entry minutes {lo:g}-{hi:g}; share of market-seconds")
    print("  hour | seconds | a side in band | both under 0.55 | favorite over the band")
    for h in sorted(by):
        n, a, b, c = by[h]
        print(f"   {h}  | {n:7d} | {a / n:13.0%} | {b / n:14.0%} | {c / n:10.0%}")
print("DONE")
