"""Every open market's risk attributes, recorded every 30 s, and what the tune makes of them (2026-11-17, user: "Are we recording every risk attribute now? If not we should record it"; "remove the weights that are close to zero or negative")."""
import json
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import test_risk_score as T  # noqa: E402

from kalshi_moonshot import config, riskrecord, riskscore, risktune, ticks, variants  # noqa: E402
from kalshi_moonshot.store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def clean():
    riskscore._cache.update(data=None, mtime=-1.0, checked=-1e9, dir=None)
    yield
    riskscore._cache.update(data=None, mtime=-1.0, checked=-1e9, dir=None)
    ticks.mids.clear()
    ticks.history.clear()
    riskscore._prev.clear()


def test_unit_only_the_used_readings_carry_a_weight(tmp_path):
    w = dict(zip(riskscore.NAMES, riskscore.WEIGHTS))
    assert all(w[n] > 0 for n in riskscore.USED)
    assert all(w[n] == 0.0 for n in riskscore.NAMES if n not in riskscore.USED)
    for left_out in ("t15", "t30", "t60", "flips", "nudge", "trend3", "trend15", "trend5 near"):
        assert left_out not in riskscore.USED
    (tmp_path / "risk_score.json").write_text(json.dumps({"weights": {n: 0.5 for n in riskscore.NAMES}}))  # a file from before the change, with a weight on every reading
    got = dict(zip(riskscore.NAMES, riskscore.weights(str(tmp_path))))
    assert got["cushion"] == 0.5 and got["nudge"] == 0.0 and got["t15"] == 0.0


def test_unit_a_left_out_reading_does_not_move_the_chance():
    x = [0.4, 0.3, 0.0, 0.1] + [0.0] * 11
    moved = list(x)
    for n in ("t15", "t30", "nudge", "trend15"):
        moved[riskscore.NAMES.index(n)] = 2.0
    assert riskscore.chance(moved, "yes") == pytest.approx(riskscore.chance(x, "yes"))
    moved[riskscore.NAMES.index("cushion")] = 2.0
    assert riskscore.chance(moved, "yes") > riskscore.chance(x, "yes")


def rows(n=1500, seed=7):
    """Markets whose result follows the price and the cushion, and also t15 (left out of the sum)."""
    rng = random.Random(seed)
    i15 = riskscore.NAMES.index("t15")
    out = []
    for i in range(n):
        x = [rng.uniform(-2, 2), rng.uniform(-1, 1)] + [0.0] * 13
        x[i15] = rng.uniform(-0.5, 0.5)
        p = 1 / (1 + 2.718281828 ** -(1.0 * x[0] + 0.8 * x[1] + 2.0 * x[i15]))
        out.append((f"M-{i}", float(i * 40), rng.random() < p, x))
    return out


def test_unit_a_refit_moves_only_the_used_readings(monkeypatch):
    monkeypatch.setattr(risktune, "MIN_FIT", 400)
    start = [1.0, -0.8] + [0.3] * 13  # a start with weights on everything: the left-out ones are dropped before the fit
    new, info = risktune.refit(rows(), start)
    got = dict(zip(riskscore.NAMES, new))
    assert got["cushion"] > 0.4 and info["loss_refit"] < info["loss_in_use"]
    assert all(got[n] == 0.0 for n in riskscore.NAMES if n not in riskscore.USED)


def test_unit_the_report_shows_what_a_left_out_reading_knew_beyond_the_price():
    rep = {a["name"]: a for a in risktune.attributes(rows(), riskscore.WEIGHTS)}
    assert rep["t15"]["used"] is False and rep["t15"]["pull"] == 0.0 and rep["t15"]["beyond_price"] > 0.1  # it is not in the sum, and the record shows it ran with the result
    assert rep["cushion"]["used"] is True and rep["cushion"]["pull"] > 0 and rep["cushion"]["beyond_price"] > 0.1
    assert abs(rep["t60"]["beyond_price"]) < 0.05 and rep["t60"]["spread"] == 0.0  # a reading that never moved says nothing
    assert rep["price"]["n"] == 1500
    twice = rows() + rows()
    assert risktune.attributes(twice, riskscore.WEIGHTS)[0]["n"] == 1500  # one reading per market per 30 s


def seed_market(tmp_path, monkeypatch, now):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(T.LANE.format(extra=""))
    s = config.load()
    series, ticker, close = next(m for m in riskrecord.open_markets(s, now) if m[0] == "KXBTC15M")
    opened = close - 900.0
    for ts, v in zip(*T.index(opened, now, lift=0.004)):
        ticks.history.note(variants.crypto_index("KXBTC15M"), ts, v)
    for ts, mid in T.mids(opened, now, level=0.80):
        ticks.mids.note(ticker, ts, mid - 0.005, mid + 0.005)
    return s, ticker, close


def test_unit_the_open_markets_are_named_by_the_quarter_hour(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    now = 1794894300.0 + 400.0  # 400 s into a quarter-hour
    got = riskrecord.open_markets(s, now)
    assert got and all(close == 1794894300.0 + 900.0 and tk.startswith(ser + "-") and ser.endswith("15M") for ser, tk, close in got)
    assert got == riskrecord.open_markets(s, now + 400.0) and got != riskrecord.open_markets(s, now + 600.0)


def test_integration_a_mark_is_recorded_from_memory_and_the_tune_reads_it_with_the_result(tmp_path, monkeypatch):
    now = (int(time.time() // 900) * 900) + 480.0  # eight minutes into the running quarter-hour
    s, ticker, close = seed_market(tmp_path, monkeypatch, now)
    store = Store(s.db_path)
    seen = []
    inner = store.db

    class Watch:
        def execute(_, sql, *args):
            seen.append(sql)
            return inner.execute(sql, *args)

        def __getattr__(_, name):
            return getattr(inner, name)

    store.db = Watch()
    snap = riskrecord.snapshot(s, now)
    assert not seen  # the reading itself asks the database nothing
    assert [r[0] for r in snap] == [ticker]  # the one market with a path in memory; the others have none and are left out
    _, mark, x, bid, ask = snap[0]
    assert mark % 30 == 0 and now - 30 < mark <= now and len(json.loads(x)) == len(riskscore.NAMES) and 0 < bid < ask < 1
    store.add_risk_readings(snap)
    store.add_risk_readings(snap)  # the same mark again is left
    assert [q for q in seen if "SELECT" in q.upper()] == []
    assert inner.execute("SELECT count(*) AS n FROM risk_readings").fetchone()["n"] == 1

    out = risktune.rebuild(s, now=now + 1200.0)
    assert out["attributes"][0]["n"] == 0  # the market has no result yet: nothing to score
    inner.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, result, fetched_ts) VALUES (?,?,?,?,?,?)", (ticker, "KXBTC15M", close, 100.0, "yes", now))
    inner.execute("INSERT OR IGNORE INTO risk_readings (ticker, ts, x, yes_bid, yes_ask) VALUES (?,?,?,?,?)", ("KXBTC15M-OLD", now - 20 * 86400.0, x, 0.5, 0.51))
    inner.commit()
    out = risktune.rebuild(s, now=now + 1200.0)
    assert out["attributes"][0]["n"] == 1 and {a["name"] for a in out["attributes"]} == set(riskscore.NAMES)
    assert out["fit"]["why"].startswith("under ")  # one reading is no fit: the weights in use stay
    assert dict(zip(riskscore.NAMES, riskscore.WEIGHTS))["cushion"] == pytest.approx(out["weights"]["cushion"], abs=1e-4) and out["weights"]["nudge"] == 0.0
    assert inner.execute("SELECT count(*) AS n FROM risk_readings").fetchone()["n"] == 1  # the reading older than the keep time was deleted


def test_integration_an_unreadable_market_writes_nothing(tmp_path, monkeypatch):
    now = (int(time.time() // 900) * 900) + 30.0  # half a minute in: too young to read
    s, ticker, close = seed_market(tmp_path, monkeypatch, now)
    assert riskrecord.snapshot(s, now) == []
    Store(s.db_path).add_risk_readings([])
