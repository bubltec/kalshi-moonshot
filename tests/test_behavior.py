"""The case memory: finished windows become saved moments with the outcome that followed."""
import json
from types import SimpleNamespace

import pytest

from kalshi_moonshot import behavior
from kalshi_moonshot.store import Store
from kalshi_moonshot.tape import Tape

CLOSE = 2_000_000.0
TICKER = "KXBTC15M-26OCT250100-00"


def _setup(tmp_path, result="yes", reach=True):
    tape = Tape(tmp_path / "tape.db")
    quotes = []
    t = CLOSE - 900.0
    while t < CLOSE:
        late = t > CLOSE - 200
        bid = 0.99 if (late and reach) else 0.79
        quotes.append((t, TICKER, bid, bid + 0.01 if bid < 0.99 else 1.0, 100, 100))
        t += 10.0
    tape.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, bid_size, ask_size) VALUES (?,?,?,?,?,?)", quotes)
    tape.db.commit()
    store = Store(tmp_path / "main.db")
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, result, fetched_ts) VALUES (?,?,?,?,?,?)", (TICKER, "KXBTC15M", int(CLOSE), 100.0, result, CLOSE))
    store.db.commit()
    s = SimpleNamespace(tape_path=str(tmp_path / "tape.db"), db_path=str(tmp_path / "main.db"), exclude_versions=frozenset(), forecast_weights=str(tmp_path / "none.json"), data_dir=str(tmp_path))
    return s, store


def test_a_finished_window_is_saved_with_what_became_of_it(tmp_path):
    s, store = _setup(tmp_path)
    added = behavior.build_cases(s)
    assert added > 10
    rows = store.db.execute("SELECT * FROM behavior_cases WHERE kind='moment' AND side='yes' ORDER BY ts").fetchall()
    assert rows and all(r["won"] == 1 and r["reach99"] == 1 for r in rows)
    assert rows[0]["minutes_to_99"] > rows[-1]["minutes_to_99"] >= 0  # nearer the end, less time to wait
    assert json.loads(rows[0]["features"])["ask"] == pytest.approx(0.80)
    assert store.db.execute("SELECT COUNT(*) FROM behavior_cases WHERE side='no'").fetchone()[0] == 0  # the NO side was priced 20c, outside the band


def test_a_window_is_not_added_twice(tmp_path):
    s, _ = _setup(tmp_path)
    first = behavior.build_cases(s)
    assert first > 0 and behavior.build_cases(s) == 0


def test_a_loser_is_recorded_as_one(tmp_path):
    s, store = _setup(tmp_path, result="no", reach=False)
    behavior.build_cases(s)
    rows = store.db.execute("SELECT won, reach99, low_bid FROM behavior_cases WHERE kind='moment' AND side='yes'").fetchall()
    assert rows and all(r["won"] == 0 and r["reach99"] == 0 for r in rows) and rows[0]["low_bid"] == pytest.approx(0.79)


def test_the_report_waits_for_enough_windows(tmp_path):
    s, _ = _setup(tmp_path)
    behavior.build_cases(s)
    assert "too few to rank features" in "\n".join(behavior.report(s))


def test_the_skip_rules_wait_for_enough_windows(tmp_path):
    s, _ = _setup(tmp_path)
    s.data_dir = str(tmp_path)
    behavior.build_cases(s)
    assert json.load(open(tmp_path / "forecast_gates.json"))["gates"] == {}  # one window is far under the minimum
