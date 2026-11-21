"""STUDY CACHE LOADER (2026-11-21): reads the files scripts/study_cache_build.py wrote (one runner task) and rebuilds the model's rows on the Mac, so a fit or a table is a local run of seconds, not a runner task of minutes.

    import study_cache as sc                       # scripts/ on the path
    cache = sc.Cache("job-results/<id>")           # the folder cloud_job.sh pulled the build into
    S = sc.side_rows(cache, kind="commodity")      # list of (t, side_vector, nets, ask, ticker, side, left_s, won, close, kind), sorted by t, gated like the model's training rows
    S = sc.side_rows(cache, kind=None, gate=False) # every side of every reading, crypto and commodity, ungated

The readings are binaryrisk.readings() from the code on this Mac (src/kalshi_moonshot), read from the cached ticks exactly as the live curve and the runner's studies read them. The first call builds the readings for every window (about a minute with 4 processes) and keeps them next to the cache in derived-<hash>.pkl; the hash covers the cache build and the source of binaryrisk.readings/shape, so a change to a reading rebuilds, anything else loads in a few seconds. Needs the project's .venv python (binaryrisk imports strategy, which imports httpx). Quotes and ticks are the runner's, so nothing here reads a database.
"""
import array, bisect, hashlib, inspect, json, multiprocessing, os, pickle, sys, time, zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from kalshi_moonshot import binaryrisk as br  # noqa: E402
from kalshi_moonshot.strategy import fee_per_contract as fee  # noqa: E402

STEP = 60.0
_G: dict = {}


def _times(raw: bytes) -> list[float]:
    a = array.array("q"); a.frombytes(raw)
    out, cur = [], 0
    for d in a:
        cur += d; out.append(cur / 1e6)
    return out


class Cache:
    def __init__(self, folder: str | Path):
        self.dir = Path(folder)
        if not (self.dir / "study_cache-meta.json").exists() and (self.dir / "files" / "study_cache-meta.json").exists():
            self.dir = self.dir / "files"  # the job folder cloud_job.sh pulled: the cache is in its files/
        self.meta = json.loads((self.dir / "study_cache-meta.json").read_text())
        self._parts: dict[str, dict] = {}
        self._where = {e: p["file"] for p in self.meta["parts"] for e in p["entries"]}
        self.windows = self.entry("windows")
        self._ticks: dict[str, tuple[list[float], list[float]]] = {}

    def entry(self, key: str):
        f = self._where[key]
        if f not in self._parts:
            self._parts[f] = pickle.loads(zlib.decompress((self.dir / f).read_bytes()))
        return self._parts[f][key]

    def ticks(self, index: str) -> tuple[list[float], list[float]]:
        if index not in self._ticks:
            T: list[float] = []; V: list[float] = []
            for k in sorted(k for k in self._where if k.startswith(f"ticks:{index}:")):
                e = self.entry(k)
                T.extend(_times(e["t"]))  # each chunk's deltas start from 0
                v = array.array("d"); v.frombytes(e["v"]); V.extend(v)
            self._ticks[index] = (T, V)
        return self._ticks[index]

    def quotes(self, w: dict) -> tuple[list[float], list[float], list[float]]:
        e = self.entry(w["qbatch"])[w["ticker"]]
        b = array.array("H"); b.frombytes(e["bid"]); a = array.array("H"); a.frombytes(e["ask"])
        return _times(e["t"]), [x / 10000.0 for x in b], [x / 10000.0 for x in a]


def _moments_for(index: str) -> list[dict]:
    cache: Cache = _G["cache"]
    T, V = cache.ticks(index)
    out = []
    for w in cache.windows:
        if w["index"] != index or not w["strike"]: continue
        close = w["close"]; opened = close - 900.0
        qt, qb, qa = cache.quotes(w)

        def quote_at(t: float):
            j = bisect.bisect_right(qt, t) - 1
            return (qb[j], qa[j]) if j >= 0 and t - qt[j] <= 60.0 else None

        t = opened + STEP
        while t <= close - 15.0:
            qq = quote_at(t)
            f = br.readings(T, V, t, w["strike"], close - t, (w["ref24"], w["ref72"])) if qq else None
            if f and qq:
                ahead = [quote_at(t + h) if t + h <= close - 15.0 else None for h in br.HORIZONS]
                out.append({"t": t, "ticker": w["ticker"], "series": w["series"], "kind": w["kind"], "close": close, "yes": w["result"] == "yes", "f": f, "q": qq, "ahead": ahead})
            t += STEP
    return out


def moments(cache: Cache, workers: int = 4) -> list[dict]:
    """Every (window, minute) the model can read: the readings dict f, the quote then, the quotes 1/2/3/5 minutes on. Built once, kept in derived-<hash>.pkl."""
    src = inspect.getsource(br.readings) + inspect.getsource(br.shape) + repr(br.HORIZONS) + repr(br.SHAPE_BINS) + str(cache.meta["built_at"]) + str(STEP)
    path = cache.dir / f"derived-{hashlib.sha1(src.encode()).hexdigest()[:12]}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    t0 = time.time()
    _G["cache"] = cache
    indexes = sorted({w["index"] for w in cache.windows})
    with multiprocessing.get_context("fork").Pool(workers) as pool:
        got = [m for ms in pool.imap_unordered(_moments_for, indexes) for m in ms]
    got.sort(key=lambda m: (m["t"], m["ticker"]))
    path.write_bytes(pickle.dumps(got, protocol=5))
    print(f"study_cache: built {len(got)} readings in {time.time() - t0:.0f}s -> {path.name}", file=sys.stderr)
    return got


def side_rows(cache: Cache, kind: str | None = "commodity", gate: bool = True, workers: int = 4, tie: str | int = "ticker") -> list[tuple]:
    """(t, side_vector, nets per $1 for hold/1m/2m/3m/5m, ask, ticker, side, seconds left, won 1/0, close, kind), sorted by t: the row the fit search scripts use. gate=True drops the sides binaryrisk.gate refuses (the model's own training cut)."""
    rows = []
    for m in moments(cache, workers):
        if kind and m["kind"] != kind: continue
        f, (bid, ask_y) = m["f"], m["q"]
        left = m["close"] - m["t"]
        for side, ask in (("yes", ask_y), ("no", 1.0 - bid)):
            if not 0.02 <= ask <= 0.98: continue
            if gate and br.gate(f, side, left): continue
            won = m["yes"] if side == "yes" else not m["yes"]
            pays = [1.0 if won else 0.0]
            for e in m["ahead"]:
                if e is None: pays.append(1.0 if won else 0.0)
                else:
                    sell = e[0] if side == "yes" else 1.0 - e[1]
                    pays.append(sell - fee(sell))
            rows.append((m["t"], br.side_vector(f, side, ask), [(p - ask - fee(ask)) / ask for p in pays], ask, m["ticker"], side, left, 1.0 if won else 0.0, m["close"], m["kind"]))
    # Markets tick on the same minute grid, so many rows share a time and a fit's every-third / every-fourth subsample depends on their order. The runner scripts get an arbitrary order from imap_unordered; here it is fixed (tie="ticker") or shuffled by seed (tie=<int>) to show how much that moves a result.
    if isinstance(tie, int):
        import random
        random.Random(tie).shuffle(rows)
        rows.sort(key=lambda r: r[0])
    else:
        rows.sort(key=lambda r: (r[0], r[4], r[5]))
    return rows


if __name__ == "__main__":
    c = Cache(sys.argv[1])
    t0 = time.time()
    S = side_rows(c, kind=None)
    print(f"{len(c.windows)} windows, {len(S)} gated sides, newest close {c.meta['newest_close_iso']}, {time.time() - t0:.1f}s")
