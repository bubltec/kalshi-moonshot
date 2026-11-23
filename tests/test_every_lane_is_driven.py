"""2026-11-23: the all-markets scan is gone and there is no two-level split. A lane that sets no review_minutes (PriskP) is watched by the entry thread and kept by scope and review jobs like every other lane."""
from kalshi_moonshot import config, entrywatch, lanes
from kalshi_moonshot.store import Store
from kalshi_moonshot.worker import Worker

LANE = "[module.m]\nsteps=['price']\n\n[lane.plain]\nestimator='market'\nprice=[0.30,0.85]\nhours=[0.0,0.26]\nsides=['yes','no']\nseries=['KXBTC15M']\nbuy_module='m'\n"


class Client:
    authed = True


def test_a_lane_with_no_review_minutes_gets_the_entry_thread_and_the_fallback_timers(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE)
    s = config.load()
    spec = lanes.lane_specs(s)["plain"]
    assert spec.review_minutes == 1.0 and spec.scope_minutes == 2.0  # unset means 1 and 2 minutes, used only while the feed is down
    assert not hasattr(lanes, "tiered") and not hasattr(lanes, "main_lanes")
    assert entrywatch.wanted(s)  # the entry thread runs for it
    w = Worker(s, Client(), None, Store(tmp_path / "m.db", frozenset()), None)
    w._ensure_lane_jobs()
    kinds = {k for k, _, _ in w.jobs.counts()}
    assert {"lane_scope:plain", "lane_review:plain"} <= kinds
    assert "scan" not in kinds


def test_the_all_markets_scan_is_gone():
    from kalshi_moonshot import runner, worker

    for name in ("candidate_markets", "shortlists", "describe_scan", "moonshot_shots", "steady_shots"):
        assert not hasattr(runner, name)
    assert not hasattr(worker.Worker, "do_scan")
