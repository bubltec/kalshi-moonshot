"""A small persistent job queue in the same SQLite file.

Why: a scan is many slow steps (fetch pages, then one model call per market). If the bot is
stopped or one step fails, the finished steps stay finished and the rest resume.

Lifecycle: pending -> running -> done | pending again (retry with backoff) | dead.
A pending job past its expires_ts becomes `expired` instead of running on stale prices.

Orders: a job that dies while `stage == "placing"` may or may not have sent an order, so it is
parked as dead for a human to check, never re-run automatically.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from .store import Store

BACKOFF_BASE = 30.0  # seconds before the first retry; doubles each attempt
BACKOFF_CAP = 900.0
PLACING = "placing"


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    created_ts: float = 0.0

    @property
    def last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


class Jobs:
    def __init__(self, store: Store):
        self.db = store.db
        self._claim_lock = threading.Lock()  # several workers claim from one queue: the pick and the mark-as-running must not interleave

    # ---- producing ----
    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        run_at: float | None = None,
        expires_in: float | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 3,
        priority: int = 0,
    ) -> int | None:
        """Add a job. Returns None when an unfinished job with the same dedupe_key exists."""
        now = time.time()
        if dedupe_key:
            dup = self.db.execute(
                "SELECT 1 FROM jobs WHERE dedupe_key=? AND status IN ('pending','running')",
                (dedupe_key,),
            ).fetchone()
            if dup:
                return None
        cur = self.db.execute(
            """INSERT INTO jobs (kind, payload, run_at, created_ts, expires_ts, dedupe_key, max_attempts, priority)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                kind, json.dumps(payload or {}), now if run_at is None else run_at, now,
                None if expires_in is None else now + expires_in, dedupe_key, max_attempts, priority,
            ),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def ensure(self, kind: str, run_at: float, **kw: Any) -> int | None:
        """Make sure one pending job of this kind exists. Used to keep recurring work alive.

        If one is already waiting, it is pulled forward when `run_at` is sooner, never delayed.
        """
        row = self.db.execute(
            "SELECT id, run_at FROM jobs WHERE kind=? AND status='pending'", (kind,)
        ).fetchone()
        if row:
            if row["run_at"] > run_at:
                self.db.execute("UPDATE jobs SET run_at=? WHERE id=?", (run_at, row["id"]))
                self.db.commit()
            return None
        return self.enqueue(kind, run_at=run_at, **kw)

    def run_now(self, kind: str, priority: int = 0) -> None:
        """Pull every pending job of this kind forward to now (or add one)."""
        now = time.time()
        cur = self.db.execute(
            "UPDATE jobs SET run_at=? WHERE kind=? AND status='pending'", (now, kind)
        )
        self.db.commit()
        if not cur.rowcount:
            self.enqueue(kind, run_at=now, priority=priority)

    # ---- consuming ----
    def claim(self, now: float | None = None, *, kinds: Collection[str] | None = None, exclude: Collection[str] | None = None) -> Job | None:
        """Take the next due job, only of these kinds if given and never of the excluded ones. Expired ones are dropped on the way."""
        with self._claim_lock:
            return self._claim(now, kinds, exclude)

    def _claim(self, now: float | None, kinds: Collection[str] | None, exclude: Collection[str] | None) -> Job | None:
        now = time.time() if now is None else now
        self.db.execute(
            """UPDATE jobs SET status='expired', finished_ts=?
               WHERE status='pending' AND expires_ts IS NOT NULL AND expires_ts<?""",
            (now, now),
        )
        cond = ""
        args: list[Any] = [now]
        for names, word in ((kinds, "IN"), (exclude, "NOT IN")):
            if names:
                cond += f" AND kind {word} ({','.join('?' for _ in names)})"
                args += sorted(names)
        row = self.db.execute(
            f"SELECT * FROM jobs WHERE status='pending' AND run_at<=?{cond} ORDER BY priority DESC, run_at, id LIMIT 1",
            args,
        ).fetchone()
        if row is None:
            self.db.commit()
            return None
        self.db.execute(
            "UPDATE jobs SET status='running', started_ts=?, attempts=attempts+1, stage=NULL WHERE id=?",
            (now, row["id"]),
        )
        self.db.commit()
        return Job(
            row["id"], row["kind"], json.loads(row["payload"] or "{}"),
            row["attempts"] + 1, row["max_attempts"], row["created_ts"],
        )

    def checkpoint(self, job_id: int, stage: str) -> None:
        self.db.execute("UPDATE jobs SET stage=? WHERE id=?", (stage, job_id))
        self.db.commit()

    def complete(self, job_id: int, result: str = "") -> None:
        self.db.execute(
            "UPDATE jobs SET status='done', finished_ts=?, result=?, last_error=NULL WHERE id=?",
            (time.time(), result[:300], job_id),
        )
        self.db.commit()

    def fail(self, job: Job, error: str) -> str:
        """Retry with exponential backoff, or park as dead after the last attempt.

        Returns 'retry' or 'dead'.
        """
        now = time.time()
        row = self.db.execute("SELECT stage FROM jobs WHERE id=?", (job.id,)).fetchone()
        if row and row["stage"] == PLACING:  # an order may have gone out: never re-run blindly
            error = f"interrupted while placing an order; check Kalshi before re-running ({error})"
            self.db.execute(
                "UPDATE jobs SET status='dead', finished_ts=?, last_error=? WHERE id=?",
                (now, error[:300], job.id),
            )
            self.db.commit()
            return "dead"
        if job.last_attempt:
            self.db.execute(
                "UPDATE jobs SET status='dead', finished_ts=?, last_error=? WHERE id=?",
                (now, error[:300], job.id),
            )
            self.db.commit()
            return "dead"
        delay = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (job.attempts - 1)) * (1 + random.random() * 0.25)
        self.db.execute(
            "UPDATE jobs SET status='pending', run_at=?, last_error=?, stage=NULL WHERE id=?",
            (now + delay, error[:300], job.id),
        )
        self.db.commit()
        return "retry"

    def defer(self, job: Job, delay: float, reason: str = "") -> None:
        """Try again later without using up an attempt (rate limits are not the job's fault)."""
        self.db.execute(
            "UPDATE jobs SET status='pending', run_at=?, attempts=attempts-1, last_error=?, stage=NULL WHERE id=?",
            (time.time() + delay, reason[:300], job.id),
        )
        self.db.commit()

    # ---- startup, housekeeping, inspection ----
    def recover(self) -> tuple[int, int]:
        """After a stop or crash: running jobs go back to pending, except ones mid-order.

        Returns (resumed, parked).
        """
        now = time.time()
        parked = self.db.execute(
            """UPDATE jobs SET status='dead', finished_ts=?,
                 last_error='interrupted while placing an order; check Kalshi before re-running'
               WHERE status='running' AND stage=?""",
            (now, PLACING),
        ).rowcount
        resumed = self.db.execute(
            "UPDATE jobs SET status='pending', run_at=?, stage=NULL WHERE status='running'", (now,)
        ).rowcount
        self.db.commit()
        return resumed, parked

    def cleanup(self, older_than_days: float = 7.0) -> int:
        cutoff = time.time() - older_than_days * 86_400
        n = self.db.execute(
            "DELETE FROM jobs WHERE status IN ('done','expired') AND finished_ts<?", (cutoff,)
        ).rowcount
        self.db.commit()
        return n

    def counts(self) -> list[tuple[str, str, int]]:
        rows = self.db.execute(
            "SELECT kind, status, COUNT(*) AS n FROM jobs GROUP BY kind, status ORDER BY kind, status"
        ).fetchall()
        return [(r["kind"], r["status"], r["n"]) for r in rows]

    def dead(self, limit: int = 20) -> list[Any]:
        return self.db.execute(
            "SELECT * FROM jobs WHERE status='dead' ORDER BY finished_ts DESC LIMIT ?", (limit,)
        ).fetchall()

    def retry_dead(self) -> tuple[int, int]:
        """Requeue dead jobs. Ones parked mid-order are left alone. Returns (requeued, skipped)."""
        skipped = self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='dead' AND last_error LIKE 'interrupted while placing%'"
        ).fetchone()[0]
        n = self.db.execute(
            """UPDATE jobs SET status='pending', attempts=0, run_at=?, finished_ts=NULL
               WHERE status='dead' AND (last_error IS NULL OR last_error NOT LIKE 'interrupted while placing%')""",
            (time.time(),),
        ).rowcount
        self.db.commit()
        return n, skipped
