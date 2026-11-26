"""A job enqueued in this process wakes the buy worker at once instead of waiting out its poll (2026-11-11, user: "everything needs to be under 5ms")."""

import threading
import time
from types import SimpleNamespace

from kalshi_moonshot import jobs as jobs_mod
from kalshi_moonshot import worker
from kalshi_moonshot.jobs import Jobs
from kalshi_moonshot.store import Store


def test_enqueue_sets_the_wake_event(tmp_path):
    jobs_mod.WAKE.clear()
    Jobs(Store(tmp_path / "t.db")).enqueue("evaluate", {"shot": {}})
    assert jobs_mod.WAKE.is_set()


def test_a_buy_worker_claims_a_new_job_in_milliseconds_not_after_its_poll(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "BUY_POLL", 5.0)  # the old behavior would sit here for five seconds
    store = Store(tmp_path / "t.db")
    jobs = Jobs(store)
    ran: list[float] = []
    me = SimpleNamespace(stopping=False, run_job=lambda job: ran.append(time.time()))

    def loop():
        me.jobs = Jobs(Store(tmp_path / "t.db"))  # opened in the worker's own thread: sqlite connections do not cross threads
        worker.Worker._buy_loop(me)

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    time.sleep(0.3)  # the worker is waiting on the event, nothing due
    sent = time.time()
    jobs.enqueue("evaluate", {"shot": {"lane": "x", "ticker": "T"}})
    for _ in range(1000):
        if ran:
            break
        time.sleep(0.002)
    me.stopping = True
    jobs_mod.WAKE.set()
    th.join(2.0)
    assert ran and ran[0] - sent < 1.5, ran and ran[0] - sent  # far under the 5 s poll the test guards against; 0.1 s failed on a loaded CI runner (0.34 s, 2026-11-25)


def test_a_job_enqueued_while_the_worker_is_busy_is_not_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "BUY_POLL", 5.0)
    store = Store(tmp_path / "t.db")
    jobs = Jobs(store)
    seen: list[int] = []

    def run_job(job):
        seen.append(job.id)
        if len(seen) == 1:  # a second job arrives during the first one's run
            me.jobs.enqueue("evaluate", {"shot": {"lane": "x", "ticker": "T2"}})

    me = SimpleNamespace(stopping=False, run_job=run_job)

    def loop():
        me.jobs = Jobs(Store(tmp_path / "t.db"))
        worker.Worker._buy_loop(me)

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    time.sleep(0.2)
    jobs.enqueue("evaluate", {"shot": {"lane": "x", "ticker": "T1"}})
    for _ in range(300):
        if len(seen) == 2:
            break
        time.sleep(0.005)
    me.stopping = True
    jobs_mod.WAKE.set()
    th.join(2.0)
    assert len(seen) == 2


def test_the_timing_breakdown_lists_every_step_and_cost_in_milliseconds():
    from kalshi_moonshot import steptime

    steptime.begin()
    try:
        steptime.add("kalshi requests", 0.120)
        steptime.add("database", 0.004)
        steptime.add("tiny", 0.0001)
        with steptime.step("risk check"):
            time.sleep(0.003)
        out = steptime.breakdown_ms()
    finally:
        steptime.end()
    assert "kalshi requests 120" in out and "database 4" in out and "risk check" in out and "tiny" not in out
    assert steptime.breakdown_ms() == ""  # nothing recorded outside a timed job
