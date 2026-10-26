"""The vector memory: a finished window becomes vector points at every scale, with what became of them."""
import json
import math
from types import SimpleNamespace

from kalshi_moonshot import vectors
from kalshi_moonshot.store import Store
from kalshi_moonshot.tape import Tape

CLOSE = 5_000_000.0
TICKER = "KXBTC15M-26OCT250100-00"


def _setup(tmp_path):
    tape = Tape(tmp_path / "tape.db")
    t = CLOSE - 900 - 4.5 * 3600
    ticks, quotes = [], []
    while t <= CLOSE:
        v = 100.0 * math.exp(0.0002 * ((t - (CLOSE - 900)) / 60) + 0.0003 * math.sin(t / 53.0))
        ticks.append((t, "BRTI", v, None, None, None))
        if t >= CLOSE - 900:
            quotes.append((t, TICKER, 0.70, 0.72, 100, 100))
        t += 1.0
    tape.db.executemany("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", ticks)
    tape.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, bid_size, ask_size) VALUES (?,?,?,?,?,?)", quotes)
    tape.db.commit()
    store = Store(tmp_path / "main.db")
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, result, fetched_ts) VALUES (?,?,?,?,?,?)", (TICKER, "KXBTC15M", int(CLOSE), 100.0, "yes", CLOSE))
    store.db.commit()
    s = SimpleNamespace(tape_path=str(tmp_path / "tape.db"), db_path=str(tmp_path / "main.db"), exclude_versions=frozenset())
    return s, store


def test_a_finished_window_becomes_a_vector_point_per_decision_minute(tmp_path):
    s, store = _setup(tmp_path)
    assert vectors.build(s) == 8  # minutes 5 to 12
    rows = store.db.execute("SELECT minute, vec, y, bid, ask FROM curve_vectors ORDER BY minute").fetchall()
    assert [r["minute"] for r in rows] == list(range(5, 13)) and all(r["y"] == 1 for r in rows)
    v = json.loads(rows[3]["vec"])  # minute 8
    assert len(v["coin"]) == 8 and len(v["fine"]) == 12 and len(v["ctx"]) == 11 and len(v["kal"]) == 8 and len(v["kfine"]) == 12
    assert v["kal"][0] == 0.71 and rows[3]["ask"] == 0.72
    assert v["coin"][-1] > 0  # the index drifted up from the target


def test_a_window_is_not_added_twice(tmp_path):
    s, _ = _setup(tmp_path)
    assert vectors.build(s) == 8 and vectors.build(s) == 0
