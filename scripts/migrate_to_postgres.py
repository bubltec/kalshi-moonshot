"""Copy the bot's SQLite databases into Postgres, check the copy, and leave the SQLite files untouched.

    DATABASE_URL=postgresql://... python scripts/migrate_to_postgres.py data/backups/moonshot-DATE.db data/backups/tape-DATE.db [--wipe]

Reads the two SQLite files read-only (run it on copies, with the bot stopped, so nothing changes under it). The target schemas are
DB_SCHEMA_MAIN (default `moonshot`) and DB_SCHEMA_TAPE (default `tape`). A table that already holds rows is left alone unless --wipe is given.
After copying it resets each generated id so new rows continue after the old ones, then compares row counts and a few column sums table by table.
"""

import os
import sqlite3
import sys
import time
from pathlib import Path

os.environ.setdefault("DB_SCHEMA_MAIN", "moonshot")
os.environ.setdefault("DB_SCHEMA_TAPE", "tape")

from kalshi_moonshot import db  # noqa: E402
from kalshi_moonshot.store import Store  # noqa: E402
from kalshi_moonshot.tape import Tape  # noqa: E402

MAIN_TABLES = ["decisions", "orders", "jobs", "shard_moves", "screen_log", "shadow", "fills", "settlements", "market_info",
               "windows", "grid_obs", "grid_done", "grid_meta", "candles", "watchlist"]
TAPE_TABLES = ["quotes", "index_ticks", "feed_ticks", "flow_1s"]
IDS = {"decisions", "orders", "jobs", "shard_moves", "screen_log", "shadow"}
CHUNK = 5000


def columns(con, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def copy_table(src: sqlite3.Connection, dst, table: str, wipe: bool) -> tuple[int, int]:
    have_src = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if table not in have_src:
        print(f"  {table:12} not in the SQLite file; skipped")
        return 0, 0
    want = set(columns(dst, table))
    cols = [c for c in columns(src, table) if c in want]
    existing = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if existing and not wipe:
        print(f"  {table:12} already has {existing:,} rows in Postgres; left alone (use --wipe to replace)")
        return existing, existing
    if existing:
        dst.execute(f"DELETE FROM {table}")
        dst.commit()
    marks = ",".join("?" * len(cols))
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})"
    n = 0
    cur = src.execute(f"SELECT {', '.join(cols)} FROM {table}")
    while True:
        rows = cur.fetchmany(CHUNK)
        if not rows:
            break
        dst.executemany(sql, [tuple(r) for r in rows])
        dst.commit()
        n += len(rows)
    if table in IDS and "id" in cols:
        dst.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT COALESCE(MAX(id), 1) FROM {table}))"
        )
        dst.commit()
    return n, dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def sums_match(src: sqlite3.Connection, dst, table: str) -> str:
    nums = [r[1] for r in src.execute(f"PRAGMA table_info({table})") if r[2].upper() in {"REAL", "INTEGER"} and r[1] != "id"][:3]
    bad = []
    for c in nums:
        a = src.execute(f"SELECT COALESCE(SUM({c}), 0) FROM {table}").fetchone()[0]
        b = dst.execute(f"SELECT COALESCE(SUM({c}), 0) FROM {table}").fetchone()[0]
        if abs(float(a) - float(b)) > 1e-6 * max(1.0, abs(float(a))):
            bad.append(f"{c}: sqlite {a} vs postgres {b}")
    return "; ".join(bad)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    wipe = "--wipe" in sys.argv
    if len(args) != 2 or not db.use_postgres():
        print(__doc__)
        return 2
    main_file, tape_file = Path(args[0]), Path(args[1])
    problems = 0
    for kind, path, tables in (("main", main_file, MAIN_TABLES), ("tape", tape_file, TAPE_TABLES)):
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        if kind == "main":
            Store(path.with_name("moonshot.db"))  # creates the tables in the target schema
            dst = db.connect(path.with_name("moonshot.db"), "main", shared=True)
        else:
            Tape(path.with_name("tape.db"))
            dst = db.connect(path.with_name("tape.db"), "tape", shared=True)
        print(f"{kind}: {path} -> schema {db.schema_for(path, kind)}")
        for table in tables:
            t0 = time.time()
            n_src, n_dst = copy_table(src, dst, table, wipe)
            diff = sums_match(src, dst, table) if n_src and table in {t for t in tables} else ""
            ok = (n_src == n_dst) and not diff
            problems += 0 if ok else 1
            print(f"  {table:12} {n_src:>10,} rows -> {n_dst:>10,} {'ok' if ok else 'MISMATCH ' + diff} ({time.time() - t0:.1f}s)")
        src.close()
    if os.environ.get("DB_SCHEMA_MAIN"):
        store = Store(main_file.with_name("moonshot.db"))
        print(f"search index rebuilt: {store.rag.rebuild()} settled cases")
    print("done" if not problems else f"{problems} table(s) did not match")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
