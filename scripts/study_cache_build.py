"""STUDY CACHE BUILD (2026-11-21, for the binary risk model studies): restore the backup once, write everything a fit or a table needs to a few compressed files, so a study runs on the Mac in seconds (scripts/study_cache.py reads them) instead of one runner task per variant.

What goes in (every 15-minute window that settled after FROM, default 2026-11-09 07:00 UTC, the start the binary risk studies use):
  windows: ticker, series, kind ("commodity" = Kalshi perp reference, S&P out; "crypto" = the coin's settlement index), the curve index, close time, result, Kalshi's own strike and expiration value (windows.strike, windows.settled_value), the strike the model reads (a crypto window's own strike, a commodity's perp value at the open, binaryrisk.open_strike), the 24 h and 72 h reference values.
  ticks: each curve index's tick series (time, value) from 3,700 s before the first window to the last close. Kept as ticked (about one a second, NOT resampled: resampling to one second moves the typical-minute readings), time stored in microseconds, so the readings are the ones the model reads live.
  quotes: per window the bid and ask from 60 s before the open to the close (the scoring of the exits: held to settlement, sold at the bid 1, 2, 3, 5 minutes later), bid/ask as dollars times 10,000 (exact: the tape's prices are on the 1/10,000 grid; the build prints the largest rounding error).
Format: stdlib only. A part file is zlib(pickle({name: bytes...})) and holds array('q'/'d'/'H') bytes; parts stay under the runner's 20 MB per-file limit. study_cache-meta.json lists the parts. Files land in DATA_DIR, so the runner copies them to s3://BUCKET/jobs/<id>/files/ and cloud_job.sh pulls them to job-results/<id>/.
Run: SPOT=0 scripts/cloud_job.sh scripts/study_cache_build.py   (FROM=<epoch seconds> to change the start)"""
import array, bisect, json, os, pickle, sys, time, zlib
from datetime import datetime, timezone
from pathlib import Path
from kalshi_moonshot import binaryrisk as br, config, db
from kalshi_moonshot.perpref import index_for_series as perp_index
from kalshi_moonshot.kalshiws import index_for_series as crypto_index

t0 = time.time()
START = float(os.environ.get("FROM") or datetime(2026, 11, 9, 7, tzinfo=timezone.utc).timestamp())
OUT = Path(os.environ.get("DATA_DIR", "."))
PART_BYTES = 14_000_000
TICK_CHUNK = 400_000
QUOTE_BATCH = 300
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
tape = db.connect(s.tape_path, "tape", shared=True)
try:
    wins = st.execute("SELECT ticker, series, close_ts, strike, settled_value, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START,)).fetchall()
except Exception:  # a database from before the settled_value column: Kalshi's expiration value is then not in the cache
    wins = [{**dict(r), "settled_value": None} for r in st.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (START,)).fetchall()]
by_index = {}
for w in wins:
    if w["series"].startswith(br.NOT_COMMODITY): continue
    cix = crypto_index(w["series"])
    ix = cix or perp_index(w["series"])
    if ix: by_index.setdefault(ix, []).append((w, "crypto" if cix else "commodity"))
print(f"{sum(len(v) for v in by_index.values())} windows on {len(by_index)} curves", flush=True)


def packq(t):  # seconds -> microsecond deltas, int64
    a = array.array("q"); prev = 0
    for x in t:
        u = int(round(x * 1e6)); a.append(u - prev); prev = u
    return a.tobytes()


parts, cur, cur_size, max_err = [], {}, 0, 0.0


def flush():
    global cur, cur_size
    if not cur: return
    name = f"study_cache-part{len(parts):03d}.z"
    blob = zlib.compress(pickle.dumps(cur, protocol=4), 9)
    (OUT / name).write_bytes(blob)
    parts.append({"file": name, "bytes": len(blob), "entries": sorted(cur)})
    cur, cur_size = {}, 0


def put(key, obj):
    global cur_size
    size = len(zlib.compress(pickle.dumps(obj, protocol=4), 6))  # a close estimate of its share of the part
    if cur and cur_size + size > PART_BYTES: flush()
    cur[key] = obj; cur_size += size


meta_w, ticks_info = [], {}
for ix, items in by_index.items():
    closes = [float(w["close_ts"]) for w, _ in items]
    lo, hi = min(closes) - 900 - 3700, max(closes) + 5
    tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, lo, hi)).fetchall()
    T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
    del tk
    n = 0
    for c0 in range(0, len(T), TICK_CHUNK):
        put(f"ticks:{ix}:{n:03d}", {"t": packq(T[c0:c0 + TICK_CHUNK]), "v": array.array("d", V[c0:c0 + TICK_CHUNK]).tobytes(), "n": len(T[c0:c0 + TICK_CHUNK])}); n += 1
    ticks_info[ix] = {"n": len(T), "first": T[0] if T else None, "last": T[-1] if T else None, "chunks": n}
    batch, pending, nb = {}, [], [0]

    def close_batch():
        if not batch: return
        key = f"quotes:{ix}:{nb[0]:03d}"; nb[0] += 1
        put(key, dict(batch))
        for m in pending: m["qbatch"] = key
        batch.clear(); pending.clear()

    for w, kind in items:
        close = float(w["close_ts"]); opened = close - 900.0
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 60, close)).fetchall()
        if len(q) < 20: continue
        qa = [float(r[2]) for r in q]
        scale = 100.0 if max(qa) > 1.5 else 1.0
        bid = array.array("H", (int(round(float(r[1]) / scale * 10000)) for r in q))
        ask = array.array("H", (int(round(a / scale * 10000)) for a in qa))
        for r, b, a in zip(q, bid, ask):
            max_err = max(max_err, abs(float(r[1]) / scale - b / 10000), abs(float(r[2]) / scale - a / 10000))
        strike = float(w["strike"]) if kind == "crypto" and w["strike"] and float(w["strike"]) > 0 else br.open_strike(T, V, close)
        ref = []
        for hh in (24.0, 72.0):
            rr = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (ix, opened - hh * 3600.0, opened - hh * 3600.0 - 1800.0)).fetchone()
            ref.append(float(rr[0]) if rr else 0.0)
        m = {"ticker": w["ticker"], "series": w["series"], "kind": kind, "index": ix, "close": close, "result": w["result"], "kalshi_strike": w["strike"], "settled_value": w["settled_value"], "strike": strike, "ref24": ref[0], "ref72": ref[1], "qbatch": None}
        meta_w.append(m); pending.append(m)
        batch[w["ticker"]] = {"t": packq([float(r[0]) for r in q]), "bid": bid.tobytes(), "ask": ask.tobytes(), "n": len(q)}
        if len(batch) >= QUOTE_BATCH: close_batch()
    close_batch()
    print(f"{ix}: {len(T)} ticks, {len(items)} windows, {time.time() - t0:.0f}s", flush=True)

put("windows", meta_w)
flush()
newest = max(w["close"] for w in meta_w)
(OUT / "study_cache-meta.json").write_text(json.dumps({"built_at": time.time(), "from": START, "windows": len(meta_w), "newest_close": newest, "newest_close_iso": datetime.fromtimestamp(newest, timezone.utc).isoformat(), "max_quote_rounding_error": max_err, "ticks": ticks_info, "parts": parts, "format": "zlib(pickle({key: obj})); ticks:IX:NNN = {t: int64 microsecond deltas, v: float64, n}; quotes:IX:K = {ticker: {t, bid, ask (uint16 dollars*10000), n}}; windows = list of dicts, each naming its quote entry in qbatch"}, indent=1))
print(f"{len(meta_w)} windows, {len(parts)} parts ({sum(p['bytes'] for p in parts) / 1e6:.1f} MB), newest close {datetime.fromtimestamp(newest, timezone.utc):%m-%d %H:%MZ}, max quote rounding error {max_err:.2e}, {time.time() - t0:.0f}s")
