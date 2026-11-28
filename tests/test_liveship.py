import ast
import io
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from kalshi_moonshot import config, liveship
from kalshi_moonshot.store import Store
from kalshi_moonshot.tape import Tape
from tests.test_archive import FakeS3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

BASE = 1_800_000_000 // 60 * 60  # a minute boundary


class S3(FakeS3):
    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey()
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0])}

    class exceptions:
        class NoSuchKey(Exception):
            pass


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "m.db", frozenset())
    tape = Tape(tmp_path / "t.db")
    tape.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, last, sts, bid_size, ask_size) VALUES (?,?,?,?,?,?,?,?)",
                        [(BASE + 5 + i, "KXBTC15M-X", 0.4, 0.41, 0.4, 7, 10.0, 12.0) for i in range(10)] + [(BASE + 70, "KXBTC15M-X", 0.5, 0.51, 0.5, 7, 1.0, 1.0)])
    tape.db.executemany("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", [(BASE + 3 * i, "BRTI", 80000.5, 80001.0, None, 123) for i in range(5)])
    tape.db.commit()
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, lane, side, ask) VALUES (?,?,?,?,?,?,?)", (BASE + 10, "KXBTC15M-X", "bet", "trade", "wave_shape", "yes", 0.3))
    store.db.execute("INSERT INTO screen_log (ts, lane, ticker, side, reason) VALUES (?,?,?,?,?)", (BASE + 11, "wave_shape", "KXBTC15M-X", "no", "out at price"))
    store.db.execute("INSERT INTO orders (ts, ticker, env, count, price, fill_count, cost_usd, fee_usd) VALUES (?,?,?,?,?,?,?,?)", (BASE + 12, "KXBTC15M-X", "prod", 5, 0.3, 0, 0, 0))
    store.db.commit()
    return store, tape.db, S3()


def table_at(s3, table, minute):
    return pq.read_table(io.BytesIO(s3.objects[("b", liveship.key_for(table, minute))][0]))


def test_a_closed_minute_is_one_file_with_exactly_its_rows(env):
    store, tape_db, s3 = env
    m = BASE // 60
    assert liveship.ship_minute(s3, "b", tape_db, store.db, "quotes", m) == 10  # the row at +70 s belongs to the next minute
    t = table_at(s3, "quotes", m)
    assert t.num_rows == 10 and t.column_names == [c for c, _ in liveship.LIVE_TABLES["quotes"]]
    assert liveship.key_for("quotes", m).startswith("tape/live/quotes/day=") and liveship.key_for("quotes", m).endswith(f"m{m}.parquet")
    assert liveship.ship_minute(s3, "b", tape_db, store.db, "quotes", m + 5) == 0 and not any(f"m{m + 5}" in k for _, k in s3.objects)  # an empty minute writes nothing


def test_shipping_a_minute_twice_never_duplicates_rows(env):
    store, tape_db, s3 = env
    m = BASE // 60
    liveship.ship_minute(s3, "b", tape_db, store.db, "quotes", m)
    liveship.ship_minute(s3, "b", tape_db, store.db, "quotes", m)
    assert len(s3.objects) == 1 and table_at(s3, "quotes", m).num_rows == 10


def test_a_decision_is_shipped_again_in_the_minute_of_its_exit_and_the_newest_copy_wins(env):
    store, tape_db, s3 = env
    m = BASE // 60
    liveship.ship_minute(s3, "b", tape_db, store.db, "decisions", m)
    first = table_at(s3, "decisions", m).to_pylist()[0]
    assert first["exit_price"] is None and first["factors"] is None and first["shipped_ts"] > 0
    store.db.execute("UPDATE decisions SET exit_price = 0.6, exit_ts = ?, exit_reason = 'uptick take' WHERE id = ?", (BASE + 125, first["id"]))
    store.db.commit()
    liveship.ship_minute(s3, "b", tape_db, store.db, "decisions", m + 2)  # its exit falls in the minute two later
    assert table_at(s3, "decisions", m + 2).to_pylist()[0]["exit_price"] == 0.6
    import live_pull

    got = live_pull.read("decisions", BASE, BASE + 200, client=s3, bucket="b").to_pylist()
    assert len(got) == 1 and got[0]["exit_price"] == 0.6 and got[0]["exit_reason"] == "uptick take"


def test_a_pass_ships_only_closed_minutes_and_a_second_pass_ships_the_next_ones(env):
    store, tape_db, s3 = env
    now = BASE + 120 + liveship.GRACE + 1  # the minute at BASE+60 has just closed past the grace; BASE+120's has not
    done, n = liveship.pass_once(s3, "b", tape_db, store.db, None, now)
    assert done == BASE // 60 + 1 and n > 0
    keys = {k for _, k in s3.objects}
    assert liveship.key_for("quotes", BASE // 60) in keys and liveship.key_for("quotes", BASE // 60 + 1) in keys and liveship.key_for("quotes", BASE // 60 + 2) not in keys
    tape_db.execute("INSERT INTO quotes (ts, ticker, bid, ask, last, sts, bid_size, ask_size) VALUES (?,?,?,?,?,?,?,?)", (BASE + 130, "KXBTC15M-X", 0.4, 0.41, 0.4, 7, 1.0, 1.0))
    tape_db.commit()
    done2, _ = liveship.pass_once(s3, "b", tape_db, store.db, done, now + 60)
    assert done2 == done + 1 and liveship.key_for("quotes", done2) in {k for _, k in s3.objects}


def test_a_failed_upload_leaves_the_watermark_alone(env):
    store, tape_db, s3 = env
    s3.fail_put = True
    with pytest.raises(RuntimeError):
        liveship.pass_once(s3, "b", tape_db, store.db, None, BASE + 200)


def test_it_is_off_without_a_bucket_or_with_the_switch_off():
    s = config.load()
    mk = lambda **kw: type(s)(**{**s.__dict__, **kw})
    assert not liveship.enabled(mk(archive_bucket="")) and not liveship.enabled(mk(archive_bucket="b", live_ship=False)) and liveship.enabled(mk(archive_bucket="b", live_ship=True))


def test_the_columns_match_the_athena_tables_defined_in_infra():
    tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("infra", "archive_stack.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "LIVE_TABLES")
    # the dict spreads TAPE_TABLES for the first four, so the rest are compared by name and columns
    names = [k.value for k in node.value.keys if k is not None]
    assert names == [n for n in liveship.LIVE_TABLES if n not in liveship.TAPE]
    for k, v in zip(node.value.keys, node.value.values, strict=True):
        if k is not None:
            assert [tuple(c) for c in ast.literal_eval(v)] == liveship.LIVE_TABLES[k.value]


def test_no_hot_path_module_imports_it():
    root = Path(__file__).resolve().parents[1] / "src" / "kalshi_moonshot"
    users = [p.name for p in root.glob("*.py") if p.name != "liveship.py" and "import liveship" in p.read_text().replace("from . import liveship", "import liveship")]
    assert users == ["worker.py"], users  # only the worker starts the thread; no tick, buy or sell path touches it
    assert time.time() > 0
