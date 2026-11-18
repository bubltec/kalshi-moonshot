"""The risk score tuned from its own record: the score's weights.

2026-11-16, user: "Make sure the logic for this risk factor is well defined and can be auto tuned to match the changing markets"; and, after four hand-made conditions in one evening: "Yea do it" to letting the record set the lines and refit the weights.

Every evaluation of a lane that buys on the risk score writes its reading into the decision row it already writes (factors["risk"]: the fifteen readings, the side's chance and Kalshi's price for the side at the three looks, the ask). A pass is settled like a bet, so every reading gets a result. Nothing is read from the database per evaluation: this module runs as its own low-priority process (as staterecord and cushionmodel do) and writes data/risk_score.json, which riskscore.py reads from memory.

Tuned from the last DAYS of readings (the gap range a lane bought was tuned here too until 2026-11-18, when its lane crypto_risk_gap was retired):
  weights  a logistic refit of the readings the sum adds (riskscore.USED; the others stay at zero) on the YES result, one reading per market per 30 s, from every open market's recorded readings (riskrecord.py's risk_readings, joined to the markets' results) and the lanes' own rows. The newest fifth of the days is held out; the refit replaces the weights in use only when its held-out log-loss is no worse than theirs.
  attributes  every one of the fifteen readings against the results, in the sum or not: its spread, its pull (weight x spread) and its correlation with what the price missed (YES won less Kalshi's mid). A report, not a rule: it shows which readings matter on the current record and which pull the wrong way.
The sale's line is not tuned here: holds are not recorded per reading (that would be a write per tick).

    python -m kalshi_moonshot.risktune        # rebuild data/risk_score.json
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import riskscore

log = logging.getLogger(__name__)

DAYS = 3.0  # the readings the tune reads: recent enough to follow the market, long enough to count
REFIT_EVERY = 3600.0
MIN_FIT = 3000  # readings a refit needs
EDGE_LOGIT = math.log(0.97 / 0.03)  # the price logit at the lanes' reading limits (a mid of 0.03 or 0.97)
KEEP_DAYS = 14.0  # recorded readings older than this are deleted by the tune

_tuning: subprocess.Popen | None = None


def reading(x: Sequence[float], chances: Sequence[float], kalshi: Sequence[float], ask: float) -> dict[str, Any]:
    """What an evaluation stores of its risk reading (decision column factors, key "risk")."""
    return {"x": [round(v, 4) for v in x], "c": [round(v, 4) for v in chances], "k": [round(v, 4) for v in kalshi], "ask": round(ask, 4)}


def _sig(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def fit(data: Sequence[tuple[Sequence[float], bool]], start: Sequence[float]) -> list[float]:
    """Logistic weights by Newton's method from `start`; `data`: (readings, YES won). A tiny ridge keeps a reading that never varies from breaking the solve."""
    k = len(start)
    w = list(start)
    for _ in range(25):
        g = [0.0] * k
        h = [[0.0] * k for _ in range(k)]
        for x, won in data:
            p = _sig(sum(a * b for a, b in zip(w, x)))
            e, v = (1.0 if won else 0.0) - p, p * (1.0 - p)
            for a in range(k):
                g[a] += e * x[a]
                for b in range(a, k):
                    h[a][b] += v * x[a] * x[b]
        m = [[(h[a][b] if b >= a else h[b][a]) + (1e-6 if a == b else 0.0) for b in range(k)] + [g[a]] for a in range(k)]
        for c in range(k):
            piv = max(range(c, k), key=lambda r: abs(m[r][c]))
            m[c], m[piv] = m[piv], m[c]
            if not m[c][c]:
                continue
            for r in range(k):
                if r != c:
                    f = m[r][c] / m[c][c]
                    m[r] = [u - f * v for u, v in zip(m[r], m[c])]
        d = [m[a][k] / m[a][a] if m[a][a] else 0.0 for a in range(k)]
        w = [a + b for a, b in zip(w, d)]
        if sum(abs(x) for x in d) < 1e-6:
            break
    return w


def loss(data: Sequence[tuple[Sequence[float], bool]], w: Sequence[float]) -> float:
    if not data:
        return float("nan")
    tot = 0.0
    for x, won in data:
        p = min(1 - 1e-6, max(1e-6, _sig(sum(a * b for a, b in zip(w, x)))))
        tot -= math.log(p if won else 1.0 - p)
    return tot / len(data)


def refit(rows: Sequence[tuple[str, float, bool, Sequence[float]]], current: Sequence[float]) -> tuple[list[float] | None, dict[str, Any]]:
    """(new weights or None, what was measured). `rows`: (ticker, ts, YES won, the YES readings). One reading per market per 30 s; the newest fifth of the time span is held out; the refit is taken only when it is no worse there than the weights in use."""
    seen: set[tuple[str, int]] = set()
    data = []
    for ticker, ts, won, x in sorted(rows, key=lambda r: r[1]):
        key = (ticker, int(ts // 30))
        if key in seen or len(x) != len(riskscore.NAMES):
            continue
        seen.add(key)
        data.append((ts, tuple(x), won))
    if len(data) < MIN_FIT:
        return None, {"readings": len(data), "why": f"under {MIN_FIT} readings"}
    cut = data[0][0] + 0.8 * (data[-1][0] - data[0][0])
    train = [(x, won) for ts, x, won in data if ts <= cut]
    test = [(x, won) for ts, x, won in data if ts > cut]
    if not train or not test:
        return None, {"readings": len(data), "why": "no held-out stretch"}
    current = list(riskscore.masked(current))
    use = [i for i, n in enumerate(riskscore.NAMES) if n in riskscore.USED]

    def fit_used(d: Sequence[tuple[Sequence[float], bool]], start: Sequence[float]) -> list[float]:
        """Fit only the readings the sum adds (riskscore.USED); the rest stay at zero."""
        got = fit([([x[i] for i in use], won) for x, won in d], [start[i] for i in use])
        out = [0.0] * len(start)
        for i, v in zip(use, got):
            out[i] = v
        return out

    new = fit_used(train, current)
    was, now_, mid = loss(test, current), loss(test, new), loss(test, [1.0] + [0.0] * (len(current) - 1))
    info = {"readings": len(data), "held_out": len(test), "loss_in_use": round(was, 5), "loss_refit": round(now_, 5), "loss_kalshi": round(mid, 5)}
    if not (now_ <= was):
        return None, info | {"why": "the refit was worse held out than the weights in use"}
    return fit_used([(x, won) for _, x, won in data], new), info


def attributes(rows: Sequence[tuple[str, float, bool, Sequence[float]]], w: Sequence[float]) -> list[dict[str, Any]]:
    """Each reading against the results, used in the sum or not. spread: its standard deviation; pull: weight x spread (how far it moves the sum); beyond_price: its correlation with (YES won - Kalshi's mid), what it knew that the price did not. One reading per market per 30 s. A reading with a negative beyond_price and a positive weight (or the reverse) is pulling the wrong way on this record."""
    seen: set[tuple[str, int]] = set()
    xs, res = [], []
    for ticker, ts, won, x in rows:
        key = (ticker, int(ts // 30))
        if key in seen or len(x) != len(riskscore.NAMES):
            continue
        seen.add(key)
        xs.append(x)
        res.append((1.0 if won else 0.0) - _sig(x[0]))
    n = len(xs)
    out = []
    for i, name in enumerate(riskscore.NAMES):
        if n < 2:
            out.append({"name": name, "used": name in riskscore.USED, "n": n})
            continue
        col = [x[i] for x in xs]
        mx, mr = sum(col) / n, sum(res) / n
        sx = math.sqrt(sum((v - mx) ** 2 for v in col) / n)
        sr = math.sqrt(sum((v - mr) ** 2 for v in res) / n)
        cov = sum((a - mx) * (b - mr) for a, b in zip(col, res)) / n
        out.append({"name": name, "used": name in riskscore.USED, "n": n, "weight": round(w[i], 4), "spread": round(sx, 4), "pull": round(abs(w[i]) * sx, 4),
                    "beyond_price": round(cov / (sx * sr), 4) if sx > 0 and sr > 0 else 0.0})
    return out


def rebuild(s: Any, now: float | None = None) -> dict[str, Any]:
    """Read the last DAYS of settled risk readings of every enabled lane that buys on the score, tune the weights, and write data/risk_score.json."""
    from . import db
    from .lanes import lane_specs

    now = time.time() if now is None else now
    lanes = sorted(n for n, sp in lane_specs(s).items() if sp.enabled and sp.risk_score_buy > 0)
    out: dict[str, Any] = {"built_at": now, "days": DAYS, "lanes": lanes}
    current = riskscore.weights(str(s.data_dir))
    fits, whole, own = [], [], 0  # `own`: readings taken from the lanes' own decision rows  # `whole`: every recorded reading, the first minute and the price's edges included: what the attribute report reads
    if lanes:
        main = db.connect(s.db_path, "main", shared=True)
        marks = ",".join("?" for _ in lanes)
        for r in main.execute(f"SELECT ticker, ts, outcome, factors FROM decisions WHERE lane IN ({marks}) AND ts>? AND outcome IN ('yes','no') AND factors LIKE ?", (*lanes, now - DAYS * 86400.0, '%"risk"%')).fetchall():
            try:
                rd = json.loads(r["factors"])["risk"]
            except (ValueError, KeyError, TypeError):
                continue
            own += 1
            fits.append((r["ticker"], float(r["ts"]), r["outcome"] == "yes", rd.get("x") or ()))
    main = db.connect(s.db_path, "main", shared=True)
    try:  # every open market's reading at every 30 s mark (riskrecord.py), with the market's result: the fit's and the attribute report's main record
        for r in main.execute("SELECT r.ticker, r.ts, r.x, r.age, w.result FROM risk_readings r JOIN windows w ON w.ticker = r.ticker WHERE r.ts>? AND w.result IN ('yes','no')", (now - DAYS * 86400.0,)).fetchall():
            try:
                x = tuple(json.loads(r["x"]))
            except (ValueError, TypeError):
                continue
            whole.append((r["ticker"], float(r["ts"]), r["result"] == "yes", x))
            if x and float(r["age"] or 0.0) >= 60.0 and abs(x[0]) < EDGE_LOGIT:  # the fit takes only the moments a lane's reading can be taken at (riskscore.features' limits)
                fits.append(whole[-1])
        main.execute("DELETE FROM risk_readings WHERE ts<?", (now - KEEP_DAYS * 86400.0,))
        main.commit()
    except Exception:
        log.exception("risk tune: the recorded readings could not be read; the lanes' own rows are used alone")
    new, info = refit(fits, current)
    out |= {"readings": own, "fit": info, "attributes": attributes(whole or fits, new or current),
            "weights": dict(zip(riskscore.NAMES, [round(v, 5) for v in (new or current)]))}
    p = Path(s.data_dir) / "risk_score.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    tmp.replace(p)
    log.info("risk tune: %d readings of %s; weights %s (%s)", own, ", ".join(lanes) or "no lane", "refit" if new else "kept", info)
    return out


def maybe_refit(s: Any, now: float | None = None) -> bool:
    """Start the hourly tune as its own low-priority process. True when one was started."""
    global _tuning
    now = time.time() if now is None else now
    if _tuning is not None and _tuning.poll() is None:
        return False
    tuned = riskscore.tuned(str(s.data_dir))
    if tuned and now - float(tuned.get("built_at") or 0.0) < REFIT_EVERY:
        return False
    if _tuning is not None and now - getattr(maybe_refit, "last", 0.0) < 600.0:
        return False  # a failed run is not retried at once
    maybe_refit.last = now  # type: ignore[attr-defined]
    log.info("risk tune: rebuilding from the lane's record (separate process)")
    _tuning = subprocess.Popen([sys.executable, "-m", "kalshi_moonshot.risktune"], preexec_fn=lambda: os.nice(15))  # lowers the child's priority only
    return True


if __name__ == "__main__":
    from . import config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    got = rebuild(config.load())
    print(f"readings {got['readings']}; fit {got['fit']}")
    for a in got["attributes"]:
        if a["n"] >= 2:
            print(f"  {a['name']:12s} {'in the sum' if a['used'] else 'left out  '} weight {a['weight']:+.3f}, spread {a['spread']:.3f}, pull {a['pull']:.3f}, beyond the price {a['beyond_price']:+.3f} (n {a['n']})")
