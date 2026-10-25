import time
from datetime import UTC, datetime, timedelta

from kalshi_moonshot import config, reverse
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.lanes import SHAPE
from kalshi_moonshot.store import Store


def market(ticker="KXBTC15M-26OCT250100-00", yes_bid="0.20", yes_ask="0.22"):
    return {
        "ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": "BTC up?", "status": "active", "volume_fp": "100",
        "close_time": (datetime.now(UTC) + timedelta(minutes=8)).isoformat(), "yes_bid_dollars": yes_bid, "yes_ask_dollars": yes_ask,
    }


class Client:
    def __init__(self, m):
        self.m = m

    def market(self, ticker):
        return dict(self.m)

    def candlesticks(self, *a, **k):
        return []


class Shape:
    """Answers only after it has been asked once, like the real shape estimator."""

    def __init__(self, p_yes=0.1):
        self.asked, self.p_yes = 0, p_yes

    def screen(self, shots, markets, spec, s, now):
        self.asked += 1
        return shots if self.asked > 1 else []

    def estimate(self, client, s, shot):
        return Estimate(self.p_yes, "shape says so", "ctx")


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return config.load(), Store(tmp_path / "m.db", frozenset())


def test_only_a_stop_on_a_15_minute_window_asks_for_a_second_look(tmp_path, monkeypatch):
    s, store = setup(tmp_path, monkeypatch)
    assert reverse.request(s, store, "KXBTC15M-A-00", "yes", "crypto_favorite", 3.0, "stop-loss: down 31%")
    assert not reverse.request(s, store, "KXBTC15M-A-00", "yes", "crypto_favorite", 3.0, "stop-loss: again")  # once per market and side
    assert not reverse.request(s, store, "KXBTC15M-B-00", "yes", "x", 3.0, "peak-crash: gave back")
    assert not reverse.request(s, store, "KXBTCD-B-00", "yes", "x", 3.0, "stop-loss: hourly")
    assert not reverse.request(s, store, "KXBTC15M-C-00", "yes", reverse.LANE, 3.0, "stop-loss: itself")
    assert len(store.pending_flips()) == 1


def test_the_model_is_asked_first_and_its_answer_becomes_a_paper_bet_on_the_other_side(tmp_path, monkeypatch):
    s, store = setup(tmp_path, monkeypatch)
    m = market()
    reverse.request(s, store, m["ticker"], "yes", "crypto_favorite", 3.0, "stop-loss: down 31%")
    est = {SHAPE: Shape(p_yes=0.05)}  # the model thinks YES is very unlikely: NO (ask 0.80) is worth it
    assert reverse.process(s, Client(m), store, est) == []  # asked, no answer yet
    lines = reverse.process(s, Client(m), store, est)
    assert len(lines) == 1 and "done" in lines[0]
    row = store.db.execute("SELECT * FROM decisions WHERE lane=?", (reverse.LANE,)).fetchone()
    assert row["side"] == "no" and row["mode"] == "observe" and row["ticker"] == m["ticker"]
    assert store.pending_flips() == []


def test_an_unanswered_flip_expires_and_a_settled_market_closes_it(tmp_path, monkeypatch):
    s, store = setup(tmp_path, monkeypatch)
    reverse.request(s, store, "KXBTC15M-A-00", "yes", "x", 3.0, "stop-loss")
    out = reverse.process(s, Client(market()), store, {SHAPE: Shape()}, now_ts=time.time() + s.flip_wait_seconds + 1)
    assert "expired" in out[0]
    reverse.request(s, store, "KXBTC15M-B-00", "no", "x", 3.0, "stop-loss")
    done = dict(market("KXBTC15M-B-00"), result="yes")
    assert "closed" in reverse.process(s, Client(done), store, {SHAPE: Shape()})[0]


def test_the_flip_lane_is_never_live_and_is_not_scanned():
    s = config.load()
    from kalshi_moonshot.lanes import lane_specs

    assert not lane_specs(s)[reverse.LANE].enabled and not s.is_live(reverse.LANE)
