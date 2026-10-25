import time

from kalshi_moonshot.jobs import PLACING, Jobs
from kalshi_moonshot.store import Store


def make(tmp_path):
    return Jobs(Store(tmp_path / "m.db"))


def test_jobs_run_oldest_due_first_and_not_before_run_at(tmp_path):
    j = make(tmp_path)
    j.enqueue("later", run_at=time.time() + 600)
    first = j.enqueue("a", run_at=time.time() - 10)
    j.enqueue("b", run_at=time.time() - 5)
    assert j.claim().id == first
    assert j.claim().kind == "b"
    assert j.claim() is None  # the future one is not due


def test_dedupe_blocks_a_second_unfinished_job_only(tmp_path):
    j = make(tmp_path)
    a = j.enqueue("evaluate", dedupe_key="evaluate:T")
    assert j.enqueue("evaluate", dedupe_key="evaluate:T") is None
    j.complete(j.claim().id)
    assert a is not None and j.enqueue("evaluate", dedupe_key="evaluate:T") is not None


def test_failures_back_off_then_park_as_dead(tmp_path):
    j = make(tmp_path)
    j.enqueue("evaluate", max_attempts=2)
    job = j.claim()
    assert j.fail(job, "boom") == "retry"
    assert j.claim() is None  # backing off
    j.db.execute("UPDATE jobs SET run_at=0")
    job = j.claim()
    assert job.attempts == 2 and job.last_attempt
    assert j.fail(job, "boom again") == "dead"
    assert [r["last_error"] for r in j.dead()] == ["boom again"]


def test_defer_does_not_use_up_an_attempt(tmp_path):
    j = make(tmp_path)
    j.enqueue("scan", max_attempts=1)
    job = j.claim()
    j.defer(job, 60, "rate limited")
    j.db.execute("UPDATE jobs SET run_at=0")
    again = j.claim()
    assert again.attempts == 1  # still the first try


def test_expired_jobs_are_dropped_not_run_late(tmp_path):
    j = make(tmp_path)
    j.enqueue("evaluate", expires_in=-1)
    assert j.claim() is None
    assert ("evaluate", "expired", 1) in j.counts()


def test_running_jobs_resume_after_a_stop(tmp_path):
    j = make(tmp_path)
    j.enqueue("evaluate")
    j.claim()  # now 'running', as if the bot died here
    assert j.recover() == (1, 0)
    assert j.claim().kind == "evaluate"


def test_a_job_that_died_mid_order_is_parked_not_rerun(tmp_path):
    j = make(tmp_path)
    j.enqueue("evaluate")
    job = j.claim()
    j.checkpoint(job.id, PLACING)
    assert j.recover() == (0, 1)
    assert j.claim() is None
    assert j.retry_dead() == (0, 1)  # and retry will not touch it either


def test_an_error_after_the_order_started_goes_straight_to_dead(tmp_path):
    j = make(tmp_path)
    j.enqueue("evaluate", max_attempts=5)
    job = j.claim()
    j.checkpoint(job.id, PLACING)
    assert j.fail(job, "db write failed") == "dead"
    assert "interrupted while placing" in j.dead()[0]["last_error"]


def test_ensure_keeps_exactly_one_pending_job(tmp_path):
    j = make(tmp_path)
    assert j.ensure("scan", time.time() + 100) is not None
    assert j.ensure("scan", time.time() + 100) is None


def test_ensure_pulls_a_pending_job_forward_but_never_delays_it(tmp_path):
    j = make(tmp_path)
    later = time.time() + 600
    sooner = time.time() + 30
    j.ensure("stop_loss", later)
    j.ensure("stop_loss", sooner)
    row = j.db.execute("SELECT run_at FROM jobs WHERE kind='stop_loss'").fetchone()
    assert abs(row["run_at"] - sooner) < 1
    j.ensure("stop_loss", later)
    row = j.db.execute("SELECT run_at FROM jobs WHERE kind='stop_loss'").fetchone()
    assert abs(row["run_at"] - sooner) < 1


def test_retry_dead_requeues_ordinary_failures(tmp_path):
    j = make(tmp_path)
    j.enqueue("scan", max_attempts=1)
    j.fail(j.claim(), "x")
    assert j.retry_dead() == (1, 0)
    assert j.claim().attempts == 1


def test_claim_can_be_limited_to_some_kinds_or_exclude_others(tmp_path):
    from kalshi_moonshot.store import Store

    j = Jobs(Store(tmp_path / "m.db"))
    j.enqueue("history", {}, run_at=1.0)
    j.enqueue("evaluate", {}, run_at=2.0)
    j.enqueue("resolve", {}, run_at=3.0)
    assert j.claim(10.0, exclude={"history", "resolve"}).kind == "evaluate"
    assert j.claim(10.0, exclude={"history", "resolve"}) is None  # only maintenance is left, and it is excluded
    assert j.claim(10.0, kinds={"resolve", "history"}).kind == "history"  # the oldest of the kinds asked for
    assert j.claim(10.0, kinds={"resolve"}).kind == "resolve"
    assert j.claim(10.0) is None
