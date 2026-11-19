"""Two-level lanes are reviewed and scoped by timers (2026-11-19 outage: the socket entry watcher was deleted, and a lane the stream 'drove' had no timer, so after the 11:15 ET restart no lane reviewed past its scope pass and no bet or pass was logged for an hour)."""
from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.worker import Worker


class FedCache:
    def fed(self):
        return True  # the websocket is up: this is the state in which the lanes used to be 'driven' and had no timer


class Client:
    authed = True
    cache = FedCache()


def test_a_lane_is_never_left_to_the_stream_alone_even_with_the_socket_up(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[module.m]\nsteps=['price']\n\n[lane.a]\nestimator='market'\nprice=[0.10,0.95]\nhours=[0.0,1.0]\nsides=['yes','no']\nseries=['KXGOLD15M']\ntimed_suffix='15M'\nmax_spread=0.05\nmin_volume=0\nlabel='a'\nbuy_module='m'\nscope_minutes=30\nreview_minutes=1\n")
    w = Worker(config.load(), Client(), None, Store(tmp_path / "m.db", frozenset()), None)
    from kalshi_moonshot.lanes import lane_specs, tiered

    spec = lane_specs(config.load())["a"]
    assert tiered(spec) and w._driven(spec) is False
    w._ensure_tiered()
    kinds = {r[0] for r in w.jobs.store.db.execute("SELECT kind FROM jobs").fetchall()} if hasattr(w.jobs, "store") else {k for k, _, _ in w.jobs.counts()}
    assert {"lane_scope:a", "lane_review:a"} <= kinds  # the timer jobs exist for the lane
