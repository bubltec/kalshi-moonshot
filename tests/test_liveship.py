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
    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k} for (b, k) in sorted(self.objects) if b == Bucket and k.startswith(Prefix)], "IsTruncated": False}

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
    liveship._tape_on = True
    liveship._tee_first = 0.0
    liveship._bets.clear()
    for t in liveship._buf:
        liveship._buf[t].clear()
    tape.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, last, sts, bid_size, ask_size) VALUES (?,?,?,?,?,?,?,?)",
                        [(BASE + 5 + i, "KXBTC15M-X", 0.4, 0.41, 0.4, 7, 10.0, 12.0) for i in range(10)] + [(BASE + 70, "KXBTC15M-X", 0.5, 0.51, 0.5, 7, 1.0, 1.0)])
    tape.db.executemany("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", [(BASE + 3 * i, "BRTI", 80000.5, 80001.0, None, 123) for i in range(5)])
    tape.db.commit()
    liveship.tee({"quotes": tape.db.execute("SELECT ts, ticker, bid, ask, last, sts, bid_size, ask_size FROM quotes").fetchall(),
                  "index_ticks": tape.db.execute("SELECT ts, index_id, value, avg60, quarter_avg, src_ms FROM index_ticks").fetchall()})
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, lane, side, ask) VALUES (?,?,?,?,?,?,?)", (BASE + 10, "KXBTC15M-X", "bet", "trade", "wave_shape", "yes", 0.3))
    store.db.execute("INSERT INTO screen_log (ts, lane, ticker, side, reason) VALUES (?,?,?,?,?)", (BASE + 11, "wave_shape", "KXBTC15M-X", "no", "out at price"))
    store.db.execute("INSERT INTO orders (ts, ticker, env, count, price, fill_count, cost_usd, fee_usd) VALUES (?,?,?,?,?,?,?,?)", (BASE + 12, "KXBTC15M-X", "prod", 5, 0.3, 0, 0, 0))
    store.db.commit()
    return store, S3()


def table_at(s3, table, minute):
    return pq.read_table(io.BytesIO(s3.objects[("b", liveship.key_for(table, minute))][0]))


def test_a_closed_minute_is_one_file_with_exactly_its_rows(env):
    store, s3 = env
    m = BASE // 60
    assert liveship.ship_rows(s3, "b", "quotes", m, liveship.buffered("quotes", m)) == 10  # the row at +70 s belongs to the next minute
    t = table_at(s3, "quotes", m)
    assert t.num_rows == 10 and t.column_names == [c for c, _ in liveship.LIVE_TABLES["quotes"]]
    assert liveship.key_for("quotes", m).startswith("tape/live/quotes/day=") and liveship.key_for("quotes", m).endswith(f"m{m}.parquet")
    assert liveship.ship_rows(s3, "b", "quotes", m + 5, liveship.buffered("quotes", m + 5)) == 0 and not any(f"m{m + 5}" in k for _, k in s3.objects)  # an empty minute writes nothing


def test_shipping_a_minute_twice_never_duplicates_rows(env):
    store, s3 = env
    m = BASE // 60
    liveship.ship_rows(s3, "b", "quotes", m, liveship.buffered("quotes", m))
    liveship.ship_rows(s3, "b", "quotes", m, liveship.buffered("quotes", m))
    assert len(s3.objects) == 1 and table_at(s3, "quotes", m).num_rows == 10


def test_a_pass_ships_only_closed_minutes_and_a_second_pass_ships_the_next_ones(env):
    store, s3 = env
    now = BASE + 120 + liveship.GRACE + 1  # the minute at BASE+60 has just closed past the grace; BASE+120's has not
    done, n = liveship.pass_once(s3, "b", store.db, None, now)
    assert done == BASE // 60 + 1 and n > 0
    keys = {k for _, k in s3.objects}
    assert liveship.key_for("quotes", BASE // 60) in keys and liveship.key_for("quotes", BASE // 60 + 1) in keys and liveship.key_for("quotes", BASE // 60 + 2) not in keys
    liveship.tee({"quotes": [(BASE + 130, "KXBTC15M-X", 0.4, 0.41, 0.4, 7, 1.0, 1.0)]})
    done2, _ = liveship.pass_once(s3, "b", store.db, done, now + 60)
    assert done2 == done + 1 and liveship.key_for("quotes", done2) in {k for _, k in s3.objects}


def test_a_failed_upload_leaves_the_watermark_alone(env):
    store, s3 = env
    s3.fail_put = True
    with pytest.raises(RuntimeError):
        liveship.pass_once(s3, "b", store.db, None, BASE + 200)


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
    assert sorted(users) == ["__main__.py", "tape.py", "worker.py"], users  # the worker starts the thread and the tape writer (a background flush) hands it rows; no tick, buy or sell path touches it
    assert time.time() > 0


def test_the_bot_ships_its_own_tables_and_the_recorder_the_tape_tables(env):
    store, s3 = env
    now = BASE + 120 + liveship.GRACE + 1
    liveship.pass_once(s3, "b", store.db, None, now, "store")
    kinds = {k.split("/")[2] for _, k in s3.objects}
    assert kinds <= {"decisions", "screen_log", "orders"} and "decisions" in kinds, kinds
    s3.objects.clear()
    liveship.pass_once(s3, "b", None, None, now, "tape")
    kinds = {k.split("/")[2] for _, k in s3.objects}
    assert kinds <= set(liveship.TAPE) and "quotes" in kinds, kinds


def test_without_the_tape_role_the_tee_keeps_nothing(env):
    store, s3 = env
    liveship._tape_on = False
    for t in liveship._buf:
        liveship._buf[t].clear()
    liveship.tee({"quotes": [(BASE + 1, "KXBTC15M-X", 0.4, 0.41, 0.4, 7, 1.0, 1.0)]})
    assert liveship._buf["quotes"] == []


def test_a_restart_backfills_the_tape_minutes_since_the_newest_file_in_s3_by_index(env, tmp_path):
    """2026-11-28 (user: "It needs to backfill anyway for restarts and new images"): after a start the minutes between the newest file in S3 and the live buffer come from the database,
    by ticker and by index id (never by a scan), and are shipped as the same per-minute files."""
    store, s3 = env
    tape = Tape(tmp_path / "t2.db")
    m = BASE // 60
    rows = [(BASE + 60 * k + 5, "KXBTC15M-X", 0.4, 0.41, 0.4, 7, 1.0, 1.0) for k in range(6)]  # minutes m .. m+5
    tape.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, last, sts, bid_size, ask_size) VALUES (?,?,?,?,?,?,?,?)", rows)
    tape.db.executemany("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", [(BASE + 60 * k + 3, "BRTI", 80000.5, 80001.0, None, 123) for k in range(6)])
    tape.db.commit()
    liveship._buf["quotes"].clear()
    liveship._buf["index_ticks"].clear()
    liveship.tee({"index_ticks": [(BASE + 60 * 6 + 3, "BRTI", 80000.5, 80001.0, None, 123)]})  # the live buffer holds BRTI, and begins after minute m+5
    liveship._tee_first = BASE + 60 * 5 + 50.0  # the buffer began in minute m+5: its first whole minute is m+6
    s3.objects[("b", liveship.key_for("quotes", m + 1))] = (b"x", "x")  # the newest file in S3 is minute m+1
    now = BASE + 60 * 8 + liveship.GRACE + 1  # minutes up to m+7 are closed
    done, n = liveship.pass_once(s3, "b", store.db, None, now, "tape", tape_db=tape.db, tickers=lambda: ["KXBTC15M-X"])
    got = {k for _, k in s3.objects}
    assert liveship.key_for("quotes", m + 2) in got and liveship.key_for("quotes", m + 5) in got  # backfilled from the database
    assert liveship.key_for("quotes", m) not in got  # at or before the newest file: not re-shipped
    assert liveship.key_for("index_ticks", m + 3) in got and n > 0


def test_a_bets_exit_and_settlement_ship_after_its_decision_minute_and_the_reader_keeps_the_newest(env):
    store, s3 = env
    m = BASE // 60
    store.db.execute("UPDATE decisions SET action = 'bet' WHERE ticker = 'KXBTC15M-X'")
    store.db.commit()
    now = (m + 5) * 60.0 + 50
    liveship.pass_once(s3, "b", store.db, None, now, "store")  # the bet is read and remembered
    did = store.db.execute("SELECT id FROM decisions").fetchone()[0]
    assert did in liveship._bets
    store.db.execute("UPDATE decisions SET exit_price = 0.7, exit_ts = ?, exit_reason = 'uptick take' WHERE id = ?", (BASE + 900, did))  # an exit, 15 minutes later: outside the rewrite window
    store.db.commit()
    liveship.pass_once(s3, "b", store.db, 0 if False else m + 4, now + 60 * 20, "store")
    updates = [k for _, k in s3.objects if "/decisions/" in k and "-b" in k]
    assert updates, "the exit is shipped as an update file"
    import live_pull

    got = live_pull.read("decisions", BASE, BASE + 1200, client=s3, bucket="b").to_pylist()
    assert len(got) == 1 and got[0]["exit_price"] == 0.7


def test_a_bet_whose_id_is_far_from_the_pass_rows_is_still_shipped(env):
    """2026-11-28: a bet's id comes from a reserved pool, not from the run of pass-row ids, so an id window never held it (no bet reached S3 in the first live versions). Bets are read through their own indexes."""
    store, s3 = env
    m = BASE // 60
    store.db.execute("UPDATE decisions SET action = 'bet', id = 5 WHERE ticker = 'KXBTC15M-X'")
    store.db.execute("INSERT INTO decisions (id, ts, ticker, action, mode, lane, side) VALUES (?,?,?,?,?,?,?)", (900_000, BASE + 20, "KXETH15M-X", "pass", "trade", "wave_shape", "yes"))
    store.db.commit()
    liveship.pass_once(s3, "b", store.db, None, (m + 5) * 60.0 + 50, "store")
    import live_pull

    got = live_pull.read("decisions", BASE, BASE + 600, client=s3, bucket="b").to_pylist()
    assert {r["id"] for r in got} == {5, 900_000} and next(r for r in got if r["id"] == 5)["action"] == "bet"


def test_a_failed_backfill_does_not_stop_the_live_minutes_and_decimal_rows_are_written(env, tmp_path):
    """2026-11-28 ~14:00 ET: after the 13:06 deploy no quote reached S3 (the recorder's backfill raised every pass and took the live shipping with it). A backfill that raises is logged and the live minutes still ship; rows from a database driver may carry Decimal."""
    import decimal

    store, s3 = env
    m = BASE // 60
    liveship._buf["quotes"].clear()
    liveship._tee_first = BASE - 3600.0  # the buffer holds everything: no backfill window
    liveship.tee({"quotes": [(BASE + 5, "KXBTC15M-X", decimal.Decimal("0.4"), decimal.Decimal("0.41"), None, 7, decimal.Decimal("1"), decimal.Decimal("2"))]})
    tape = Tape(tmp_path / "t3.db")

    def boom(*a, **k):
        raise RuntimeError("the backfill broke")

    import pytest as _pt

    mp = _pt.MonkeyPatch()
    mp.setattr(liveship, "backfill_tape", boom)
    mp.setattr(liveship, "last_shipped_minute", lambda *a, **k: m - 10)  # an old file: a backfill is wanted
    liveship._tee_first = BASE + 60 * 3 + 5.0  # the buffer began later than the old file: the backfill window is non-empty
    try:
        liveship.tee({"quotes": [(BASE + 60 * 4 + 10, "KXBTC15M-X", decimal.Decimal("0.4"), decimal.Decimal("0.41"), None, 7, decimal.Decimal("1"), decimal.Decimal("2"))]})
        done, n = liveship.pass_once(s3, "b", store.db, None, BASE + 60 * 6 + liveship.GRACE + 1, "tape", tape_db=tape.db, tickers=lambda: ["KXBTC15M-X"])
    finally:
        mp.undo()
    got = {k for _, k in s3.objects}
    assert liveship.key_for("quotes", m + 4) in got and n > 0, "the live minute was not shipped after a failed backfill"
