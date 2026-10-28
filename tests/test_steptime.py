import logging
import threading
import time

from kalshi_moonshot import steptime


def test_nothing_is_recorded_until_a_job_begins():
    steptime.add("rest", 5.0)
    with steptime.step("x"):
        pass
    assert steptime.summary() == ""


def test_steps_and_costs_are_summed_and_small_ones_left_out(monkeypatch):
    steptime.begin()
    try:
        steptime.add("request wait", 30.0)
        steptime.add("request wait", 1.5)
        steptime.add("database", 0.01)
        steptime.add("kalshi requests", 8.0)
        clock = iter([0.0, 12.0, 20.0, 20.1])
        monkeypatch.setattr(steptime.time, "perf_counter", lambda: next(clock))
        with steptime.step("order book"):
            pass
        with steptime.step("risk check"):
            pass
        text = steptime.summary()
    finally:
        steptime.end()
    assert "order book 12.0s" in text and "risk check" not in text  # 0.1 s is under the 0.3 s cut
    assert "request wait 31.5s" in text and "kalshi requests 8.0s" in text and "database" not in text
    assert text.index("request wait") < text.index("kalshi requests")  # the biggest cost comes first


def test_timing_is_per_thread():
    steptime.begin()
    try:
        steptime.add("rest", 9.0)
        seen = []
        th = threading.Thread(target=lambda: (steptime.add("rest", 9.0), seen.append(steptime.summary())))
        th.start()
        th.join()
        assert seen == [""]  # the other thread never called begin()
        assert "rest 9.0s" in steptime.summary()
    finally:
        steptime.end()


def test_a_slow_job_logs_where_its_time_went(tmp_path, caplog, monkeypatch):
    from tests.test_worker import FakeClient, worker

    w = worker(tmp_path, FakeClient())
    w.start()
    w.run_job(w.jobs.claim(kinds={"scan"}))
    job = w.jobs.claim(kinds={"evaluate"})
    monkeypatch.setattr("kalshi_moonshot.worker.SLOW_JOB_SECONDS", -1.0)  # every job counts as slow
    real = w.do_evaluate

    def slow(j):
        steptime.add("request wait", 4.0)
        return real(j)

    w.do_evaluate = slow
    with caplog.at_level(logging.INFO, logger="moonshot"):
        w.run_job(job)
    assert any("slow job" in r.message and "request wait 4.0s" in r.message for r in caplog.records)
    steptime.add("rest", 1.0)  # after the job ended nothing is being timed
    assert steptime.summary() == ""


def test_the_database_wrapper_records_its_lock_wait_and_statement_time():
    class Conn:
        _lock = threading.RLock()

        @staticmethod
        def work():
            time.sleep(0.02)
            return 1

    wrapped = __import__("kalshi_moonshot.db", fromlist=["_locked"])._locked(lambda self: self.work())
    steptime.begin()
    try:
        assert wrapped(Conn()) == 1
        costs = steptime._tl.costs
    finally:
        steptime.end()
    assert costs["database"] >= 0.02 and "database lock wait" in costs

