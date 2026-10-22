"""Consistent copies of the databases, so a bad write or a bad day never costs the record.

For Postgres this is `pg_dump` of each schema (the bot's tables and the tape) in its compressed custom format, listed back to check it is whole;
restore with `pg_restore`. For SQLite it is SQLite's own backup call, which copies a database while it is in use and always yields a whole,
readable file (a plain file copy of a busy database can be torn). Each copy is checked before it replaces anything, and old copies are pruned.
Copies go to DATA_DIR/backups on the host.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import db
from .config import Settings

log = logging.getLogger("moonshot")

CHECK_EVERY = 1800.0  # seconds between looks at whether a copy is due
DUE_AFTER = 20 * 3600.0  # a copy older than this is replaced (roughly nightly)


def copy_db(src: Path, dest: Path) -> bool:
    """Back up `src` to `dest` through SQLite, check it, and move it into place. Returns True when a good copy was made."""
    if not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    a = sqlite3.connect(str(src), timeout=60)
    b = sqlite3.connect(str(tmp))
    try:
        a.backup(b)
        ok = b.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        b.close()
        a.close()
    if not ok:
        tmp.unlink(missing_ok=True)
        log.error("backup: the copy of %s failed its check; kept the previous one", src.name)
        return False
    tmp.replace(dest)
    return True


def dump_schema(schema: str, dest: Path) -> bool:
    """pg_dump one schema to `dest` (custom format), verify it can be read back, and move it into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    try:
        subprocess.run(["pg_dump", "--format=custom", f"--schema={schema}", f"--file={tmp}", db.database_url()],
                       check=True, capture_output=True, timeout=3600)
        subprocess.run(["pg_restore", "--list", str(tmp)], check=True, capture_output=True, timeout=600)
    except (subprocess.SubprocessError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        log.error("backup: dumping schema %s failed (%s); kept the previous copy", schema, getattr(exc, "stderr", exc))
        return False
    tmp.replace(dest)
    return True


def prune(folder: Path, prefix: str, keep: int) -> None:
    for old in sorted(list(folder.glob(f"{prefix}-*.db")) + list(folder.glob(f"{prefix}-*.dump")), reverse=True)[keep:]:
        old.unlink(missing_ok=True)


def newest(folder: Path, prefix: str) -> Path | None:
    found = sorted(list(folder.glob(f"{prefix}-*.db")) + list(folder.glob(f"{prefix}-*.dump")), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def run_once(s: Settings, force: bool = False) -> list[str]:
    """Back up each database whose newest copy is due. Returns what was written."""
    folder = s.data_dir / "backups"
    stamp = datetime.now().strftime("%Y%m%d")
    wrote: list[str] = []
    for name, src, keep in (("moonshot", s.db_path, s.backup_keep_days), ("tape", s.tape_path, 2)):
        last = newest(folder, name)
        if not force and last is not None and time.time() - last.stat().st_mtime < DUE_AFTER:
            continue
        if db.use_postgres():
            dest = folder / f"{name}-{stamp}.dump"
            ok = dump_schema(db.schema_for(src, "tape" if name == "tape" else "main"), dest)
        else:
            dest = folder / f"{name}-{stamp}.db"
            ok = copy_db(src, dest)
        if ok:
            prune(folder, name, keep)
            wrote.append(dest.name)
    return wrote


def start_nightly(get_settings) -> None:
    """A background thread that makes sure a recent copy of each database exists."""

    def loop() -> None:
        time.sleep(300.0)  # let the bot settle after a start
        while True:
            try:
                for name in run_once(get_settings()):
                    log.info("backup: wrote %s", name)
            except Exception:
                log.exception("backup: failed")
            time.sleep(CHECK_EVERY)

    threading.Thread(target=loop, name="backup", daemon=True).start()
