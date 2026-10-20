from datetime import UTC, datetime

import pytest

from kalshi_moonshot import history
from kalshi_moonshot.store import Store

CLOSE = int(datetime(2026, 10, 19, 15, 15, tzinfo=UTC).timestamp())  # 11:15 New York


def candle(end_ts, bid, ask, vol=100.0):
    return {"end_period_ts": end_ts, "yes_bid": {"close_dollars": f"{bid:.4f}", "low_dollars": f"{bid - 0.01:.4f}"},
            "yes_ask": {"close_dollars": f"{ask:.4f}", "high_dollars": f"{ask + 0.01:.4f}"},
            "price": {"close_dollars": f"{(bid + ask) / 2:.4f}"}, "volume_fp": str(vol), "open_interest_fp": "50"}


class Client:
    def __init__(self, paths):
        self.paths = paths  # ticker -> (result, [yes mid by elapsed minute 1..15])
        self.candle_calls = 0

    def settled_markets(self, series, limit=40):
        return [{"ticker": t, "close_time": datetime.fromtimestamp(CLOSE + i * 900, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "floor_strike": 100.0, "result": res} for i, (t, (res, _)) in enumerate(self.paths.items())]

    def _request(self, method, path, params=None, **kw):
        self.candle_calls += 1
        ticker = path.split("/")[-2]
        _res, mids = self.paths[ticker]
        close = params["end_ts"]
        return {"candlesticks": [candle(close - (15 - k) * 60, m - 0.01, m + 0.01) for k, m in enumerate(mids, 1)]}


def straight(start, end, n=15):
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def make(tmp_path, paths):
    store = Store(tmp_path / "m.db")
    client = Client(paths)
    history.ingest(client, store, ["KXGOLD15M"], windows=len(paths))
    return store, client


def test_ingest_stores_each_window_once_and_is_safe_to_rerun(tmp_path):
    paths = {f"KXGOLD15M-W{i}": ("yes", straight(0.5, 0.95)) for i in range(3)}
    store, client = make(tmp_path, paths)
    assert store.db.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 3
    assert store.db.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 45
    calls = client.candle_calls
    assert history.ingest(client, store, ["KXGOLD15M"], windows=3) == 0  # nothing new: no downloads
    assert client.candle_calls == calls


def test_load_numbers_the_minutes_from_the_open(tmp_path):
    store, _ = make(tmp_path, {"KXGOLD15M-A": ("yes", straight(0.40, 0.90))})
    (w,) = history.load(store)
    assert sorted(w.mid) == list(range(1, 16))
    assert w.mid[1] == pytest.approx(0.40) and w.mid[15] == pytest.approx(0.90)
    assert w.open_ts == CLOSE - 900 and w.spread[10] == pytest.approx(0.02)


def decided_up():  # climbs to certainty early
    return straight(0.5, 0.99, 8) + [0.99] * 7


def test_path_and_decision_timing(tmp_path):
    paths = {f"KXGOLD15M-U{i}": ("yes", decided_up()) for i in range(10)}
    paths |= {f"KXGOLD15M-Z{i}": ("no", [0.5] * 15) for i in range(10)}  # never decided
    store, _ = make(tmp_path, paths)
    wins = history.load(store)
    p = {k: (off, dec) for k, off, dec, _ in history.path(wins)}
    assert p[1][1] == 0.0 and p[12][1] == pytest.approx(0.5)  # half the windows are decided late
    by = dict(history.decision_minute(wins))
    assert by[3] == 0.0 and by[9] == pytest.approx(0.5) and by[13] == pytest.approx(0.5)


def test_favorites_count_reversals_and_the_swing_against_a_holder(tmp_path):
    # four windows with YES at 80% at minute 10; one of them collapses and loses
    holds = {f"KXGOLD15M-H{i}": ("yes", [0.8] * 15) for i in range(3)}
    flip = {"KXGOLD15M-F": ("no", [0.8] * 10 + [0.5, 0.3, 0.2, 0.1, 0.02])}
    store, _ = make(tmp_path, holds | flip)
    f = history.favorites(history.load(store), 10)
    assert f.n == 4 and f.reversed_ == pytest.approx(0.25)
    assert f.avg_price == pytest.approx(0.8)
    assert f.p10_adverse > 0.5  # the worst window fell most of the way


def test_a_no_favorite_is_scored_from_the_no_side(tmp_path):
    store, _ = make(tmp_path, {f"KXGOLD15M-N{i}": ("no", [0.2] * 15) for i in range(4)})
    f = history.favorites(history.load(store), 10)
    assert f.n == 4 and f.reversed_ == 0.0 and f.avg_price == pytest.approx(0.8)  # NO at 80%


def test_prices_outside_the_band_are_not_favorites(tmp_path):
    store, _ = make(tmp_path, {"KXGOLD15M-X": ("yes", [0.5] * 15), "KXGOLD15M-Y": ("yes", [0.97] * 15)})
    assert history.favorites(history.load(store), 10) is None


def test_by_hour_uses_new_york_time(tmp_path):
    store, _ = make(tmp_path, {f"KXGOLD15M-H{i}": ("yes", straight(0.5, 0.9)) for i in range(5)})
    rows = history.by_hour(history.load(store), "America/New_York")
    # windows close 11:15, 11:30, ... so they open from 11:00 New York
    assert rows and all(r["hour"] in (11, 12) for r in rows)
    assert sum(r["n"] for r in rows) == 5


def test_a_scheduled_run_stops_at_its_batch_and_the_rest_waits(tmp_path):
    paths = {f"KXGOLD15M-W{i}": ("yes", straight(0.5, 0.95)) for i in range(10)}
    store = Store(tmp_path / "m.db")
    client = Client(paths)
    assert history.ingest(client, store, ["KXGOLD15M"], windows=10, max_new=4) == 4
    assert history.ingest(client, store, ["KXGOLD15M"], windows=10, max_new=4) == 4
    assert history.ingest(client, store, ["KXGOLD15M"], windows=10, max_new=4) == 2
    assert store.db.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 10


def test_old_windows_and_their_candles_are_pruned(tmp_path):
    store, _ = make(tmp_path, {f"KXGOLD15M-W{i}": ("yes", straight(0.5, 0.9)) for i in range(3)})
    # windows close from 15:15 UTC on the 19th; pretend it is 40 days later
    later = CLOSE + 40 * 86400
    assert history.prune(store, keep_days=30, now=later) == 3
    assert store.db.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 0
    assert history.prune(store, keep_days=30, now=later) == 0


def test_the_bot_schedules_history_at_a_priority_below_trading(tmp_path):
    from kalshi_moonshot import config
    from kalshi_moonshot.jobs import Jobs
    from kalshi_moonshot.worker import HISTORY_PRIORITY, Worker

    s = config.load()
    s = type(s)(**{**s.__dict__, "data_dir": tmp_path, "mode": "observe", "history_enabled": True,
                   "commodities_enabled": False, "history_batch": 5})
    store = Store(tmp_path / "moonshot.db")
    w = Worker(s, Client({f"KXGOLD15M-W{i}": ("yes", straight(0.5, 0.9)) for i in range(8)}), None, store, None)
    w.start()
    row = store.db.execute("SELECT priority FROM jobs WHERE kind='history' AND status='pending'").fetchone()
    assert row["priority"] == HISTORY_PRIORITY < 0
    jobs = Jobs(store)
    store.db.execute("UPDATE jobs SET run_at=0 WHERE kind='history'")
    store.db.commit()
    while (job := jobs.claim()) and job.kind != "history":
        jobs.complete(job.id)
    w.run_job(job)
    assert store.db.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 5  # one batch
    nxt = store.db.execute("SELECT run_at FROM jobs WHERE kind='history' AND status='pending'").fetchone()
    assert nxt["run_at"] - __import__("time").time() < 130  # more waiting: back in about two minutes
