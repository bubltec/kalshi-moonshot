import ast
import base64
import hashlib
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from kalshi_moonshot import archive, config
from kalshi_moonshot.store import Store
from kalshi_moonshot.tape import Tape

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class FakeS3:
    """Just enough of the S3 client: it checks the SHA-256 the way S3 does, and can fail or store a damaged copy."""

    def __init__(self):
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.fail_put = False
        self.damage = False

    def put_object(self, Bucket, Key, Body, ChecksumSHA256, ContentType=None):
        data = Body.read()
        if self.fail_put:
            raise RuntimeError("connection reset")
        if base64.b64encode(hashlib.sha256(data).digest()).decode() != ChecksumSHA256:
            raise RuntimeError("BadDigest")
        if self.damage:
            data = data[:-1]
        self.objects[(Bucket, Key)] = (data, ChecksumSHA256)

    def head_object(self, Bucket, Key, ChecksumMode=None):
        if (Bucket, Key) not in self.objects:
            raise RuntimeError("An error occurred (404) when calling the HeadObject operation: Not Found")
        data, sha = self.objects[(Bucket, Key)]
        return {"ContentLength": len(data), "ChecksumSHA256": sha}

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self.objects[(Bucket, Key)][0])


def settings(**kw):
    s = config.load()
    return type(s)(**{**s.__dict__, "archive_bucket": "test-bucket", "archive_hot_days": 5, **kw})


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "m.db", frozenset())
    tape = Tape(tmp_path / "t.db")
    first = NOW - timedelta(days=9)
    for d in range(10):  # 9 complete days back to today's partial one
        base = (first + timedelta(days=d)).replace(hour=0, minute=0, second=0).timestamp()
        tape.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, last, sts, bid_size, ask_size) VALUES (?,?,?,?,?,?,?,?)",
                            [(base + 100 * i + 0.25, "KXBTC15M-X", 0.4, 0.41, 0.4, 7, 10.0, 12.0) for i in range(20)])
        tape.db.executemany("INSERT INTO flow_1s VALUES (?,?,?,?,?,?)", [(int(base) + 60 * i, "KXBTC15M-X", 3.0, 1.0, 2, 0.41) for i in range(5)])
        tape.db.executemany("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", [(base + 10 * i, "BRTI", 80000.5, 80001.0, None, 123) for i in range(8)])
        tape.db.executemany("INSERT INTO feed_ticks VALUES (?,?,?)", [(base + 30 * i, "kraken:BTC/USD", 80010.0) for i in range(6)])
    tape.db.commit()
    return store, tape.db, FakeS3()


def day_of(offset):
    return (NOW - timedelta(days=offset)).date().isoformat()


def test_complete_days_leave_out_today_and_the_hour_of_grace(env):
    _store, tape_db, _ = env
    days = archive.complete_days(tape_db, "quotes", NOW)
    assert days[0] == day_of(9) and days[-1] == day_of(1) and day_of(0) not in days and len(days) == 9
    assert day_of(1) not in archive.complete_days(tape_db, "quotes", datetime(2026, 11, 20, 0, 30, tzinfo=UTC))  # yesterday ended only 30 minutes ago


def test_a_dry_run_changes_nothing_and_needs_no_client(env):
    store, tape_db, s3 = env
    rep = archive.run(settings(), store, tape_db, None, apply=False, delete_local=False, now=NOW)
    text = "\n".join(rep.lines)
    assert "9 not archived yet" in text and "Parquet" in text
    assert not s3.objects and store.db.execute("SELECT COUNT(*) FROM archive_manifest").fetchone()[0] == 0
    assert archive.count_rows(tape_db, "quotes", day_of(3)) == 20


def test_apply_uploads_every_day_once_and_records_it(env):
    store, tape_db, s3 = env
    rep = archive.run(settings(), store, tape_db, s3, apply=True, delete_local=False, now=NOW)
    assert rep.uploaded == 9 * 4 and rep.failed == 0 and len(s3.objects) == 36
    m = archive.manifest(store, "quotes", day_of(2))
    assert m["rows_n"] == 20 and m["s3_key"] == f"tape/quotes/day={day_of(2)}/part-0.parquet" and m["deleted_ts"] is None
    again = archive.run(settings(), store, tape_db, s3, apply=True, delete_local=False, now=NOW)
    assert again.uploaded == 0  # idempotent
    assert archive.count_rows(tape_db, "quotes", day_of(2)) == 20  # nothing deleted without the switch


def test_the_uploaded_file_holds_exactly_the_rows_with_the_declared_types(env):
    store, tape_db, s3 = env
    archive.run(settings(), store, tape_db, s3, apply=True, delete_local=False, tables=["quotes", "flow_1s"], now=NOW)
    t = pq.read_table(io.BytesIO(s3.objects[("test-bucket", f"tape/quotes/day={day_of(4)}/part-0.parquet")][0]))
    assert t.num_rows == 20 and t.column_names == [c for c, _ in archive.TABLES["quotes"]]
    assert str(t.schema.field("ts").type) == "double" and str(t.schema.field("sts").type) == "int64"
    assert t.column("ticker").to_pylist()[0] == "KXBTC15M-X"
    f = pq.read_table(io.BytesIO(s3.objects[("test-bucket", f"tape/flow_1s/day={day_of(4)}/part-0.parquet")][0]))
    assert str(f.schema.field("ts").type) == "int64" and str(f.schema.field("n").type) == "int32"


def test_old_archived_days_are_deleted_locally_only_when_asked_and_recent_ones_stay(env):
    store, tape_db, s3 = env
    archive.run(settings(), store, tape_db, s3, apply=True, delete_local=True, now=NOW)  # hot window 5 days: days 9..6 go, 5..1 stay
    assert archive.count_rows(tape_db, "quotes", day_of(9)) == 0 and archive.count_rows(tape_db, "quotes", day_of(6)) == 0
    assert archive.count_rows(tape_db, "quotes", day_of(5)) == 20 and archive.count_rows(tape_db, "quotes", day_of(1)) == 20
    assert archive.manifest(store, "quotes", day_of(9))["deleted_ts"] is not None and archive.manifest(store, "quotes", day_of(5))["deleted_ts"] is None
    assert tape_db.execute("SELECT COUNT(*) FROM quotes WHERE ts >= ?", (archive.day_bounds(day_of(0))[0],)).fetchone()[0] == 20  # today untouched


def test_a_failed_upload_records_nothing_and_deletes_nothing(env):
    store, tape_db, s3 = env
    s3.fail_put = True
    rep = archive.run(settings(), store, tape_db, s3, apply=True, delete_local=True, now=NOW)
    assert rep.failed == 36 and rep.uploaded == 0 and rep.deleted_days == 0
    assert store.db.execute("SELECT COUNT(*) FROM archive_manifest").fetchone()[0] == 0
    assert archive.count_rows(tape_db, "quotes", day_of(9)) == 20


def test_a_damaged_copy_in_the_bucket_is_caught_before_anything_is_deleted(env):
    store, tape_db, s3 = env
    s3.damage = True
    rep = archive.run(settings(), store, tape_db, s3, apply=True, delete_local=True, now=NOW)
    assert rep.failed == 36 and rep.deleted_days == 0 and archive.count_rows(tape_db, "quotes", day_of(9)) == 20
    assert store.db.execute("SELECT COUNT(*) FROM archive_manifest").fetchone()[0] == 0


def test_late_rows_in_an_archived_day_stop_its_deletion(env):
    store, tape_db, s3 = env
    archive.run(settings(), store, tape_db, s3, apply=True, delete_local=False, now=NOW)
    tape_db.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?,?,?,?)", (archive.day_bounds(day_of(9))[0] + 5.5, "KXBTC15M-X", 0.5, 0.51))
    tape_db.commit()
    rep = archive.Report()
    assert archive.delete_local_day(store, tape_db, "quotes", day_of(9), rep) is False
    assert archive.count_rows(tape_db, "quotes", day_of(9)) == 21 and "late rows" in rep.lines[0]


def test_the_tape_cleanup_deletes_nothing_unless_the_switch_is_on(env):
    store, tape_db, s3 = env
    archive.run(settings(), store, tape_db, s3, apply=True, delete_local=False, now=NOW)
    assert archive.prune_local(settings(archive_delete_local=False), store, tape_db) == 0
    assert archive.count_rows(tape_db, "quotes", day_of(9)) == 20


def test_backups_are_copied_once(tmp_path):
    s3 = FakeS3()
    (tmp_path / "moonshot-20261120.dump").write_bytes(b"dump-bytes")
    rep = archive.Report()
    archive.upload_backups(s3, "b", tmp_path, rep)
    archive.upload_backups(s3, "b", tmp_path, rep)
    assert rep.uploaded == 1 and ("b", "backups/moonshot-20261120.dump") in s3.objects


def test_fetch_downloads_a_days_files(env, tmp_path):
    store, tape_db, s3 = env
    archive.run(settings(), store, tape_db, s3, apply=True, delete_local=False, now=NOW)
    got = archive.fetch_day(settings(), s3, day_of(3), tmp_path / "out", ["quotes"])
    assert got[0].name == f"quotes-{day_of(3)}.parquet" and pq.read_table(got[0]).num_rows == 20


def test_it_is_off_without_a_bucket():
    assert archive.enabled(settings(archive_bucket="")) is False and archive.enabled(settings()) is True


def test_the_columns_match_the_athena_tables_defined_in_infra():
    tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("infra", "archive_stack.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "TAPE_TABLES")
    assert ast.literal_eval(node.value) == {t: [list(c) for c in cols] for t, cols in archive.TABLES.items()} or \
        {t: [tuple(c) for c in cols] for t, cols in ast.literal_eval(node.value).items()} == archive.TABLES
