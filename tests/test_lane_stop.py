import time

import pytest

from kalshi_moonshot import config, lanes, runner, watcher
from kalshi_moonshot.store import Store

TOML = (
    "[lane.fav]\nestimator='ladder'\nprice=[0.5,0.9]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=5.0\n"
    "stop_loss=true\nstop_loss_at=0.6\nstop_confirm_seconds=10\nstop_guard=false\n"
)


class Client:
    def __init__(self, bid):
        self.bid = bid
        self.sold = []

    def market(self, ticker):
        return {"ticker": ticker, "yes_bid_dollars": str(self.bid), "yes_ask_dollars": str(self.bid + 0.01), "close_time": "2099-01-01T00:00:00Z"}

    def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.sold.append(price)
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "s"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    (s.data_dir / "strategies.toml").write_text(TOML)
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1.0, "KXBTC15M-1", "bet", "observe", 0.80, "yes", "fav", 6, 4.9, "2099-01-01T00:00:00Z"))
    store.db.commit()
    return s, store


def quote(bid, ts):
    return {"kind": "quote", "ticker": "KXBTC15M-1", "bid": bid, "ask": bid + 0.01, "ts": ts}


def test_a_lane_can_set_its_own_stop_instead_of_the_global_one(setup):
    s, _ = setup
    spec = lanes.lane_specs(s)["fav"]
    assert lanes.stop_drop_for(spec, s) == pytest.approx(0.6) and s.stop_loss_drop == pytest.approx(0.50)  # the global default is half
    assert lanes.stop_drop_for(None, s) == pytest.approx(0.50)


def test_the_stop_sits_where_the_lane_says_and_waits_ten_seconds(setup):
    s, _ = setup
    w = watcher.ProfitWatcher(lambda: s, Client(0.45), lambda: Store(s.db_path))
    w.refresh()
    t = w.targets[("KXBTC15M-1", "fav")]
    t.stop_from = 0.0
    assert (t.stop_drop, t.stop_confirm) == (pytest.approx(0.6), 10.0)
    base = time.time()
    t.close_ts = base + 240.0  # 4 minutes left: the calculated wait is min(the lane's 10 s, 45 s) — the cap this test is about
    w.on_event(quote(0.50, base))  # 37% under the 0.80 paid
    w.on_event(quote(0.39, base + 5))  # 51% under: the global 50% stop would sell, this lane's 60% does not
    assert w._jobs.empty()
    w.on_event(quote(0.31, base + 6))  # 61% under the line
    w.on_event(quote(0.31, base + 12))  # not yet 10 s
    assert w._jobs.empty()
    w.on_event(quote(0.30, base + 17))  # held past 10 s
    assert not w._jobs.empty() and "stop-loss" in w._jobs.get_nowait()[2]


def test_a_hard_stop_ignores_the_models_opinion_but_a_guarded_one_does_not(setup):
    s, _ = setup
    why = "stop-loss: bid 0.36 is 55% under the 0.80 paid (socket)"
    c = Client(0.36)
    w = watcher.ProfitWatcher(lambda: s, c, lambda: Store(s.db_path), hold_value=lambda row, st: 0.70)  # the model says it is worth 70c
    w.refresh()
    w._sell("KXBTC15M-1", "fav", why)
    assert len(Store(s.db_path).open_bets()) == 0  # sold anyway: stop_guard is false for this lane
    (s.data_dir / "strategies.toml").write_text(TOML.replace("stop_guard=false", "stop_guard=true"))
    st = Store(s.db_path)
    st.db.execute("UPDATE decisions SET exit_ts=NULL, exit_price=NULL")
    st.db.commit()
    w2 = watcher.ProfitWatcher(lambda: s, Client(0.36), lambda: Store(s.db_path), hold_value=lambda row, sx: 0.70)
    w2.refresh()
    w2._sell("KXBTC15M-1", "fav", why)
    assert len(Store(s.db_path).open_bets()) == 1  # a guarded stop holds while the model values it above the bid


def test_the_periodic_backstop_uses_the_lanes_stop_too(setup):
    import datetime

    s, store = setup
    soon = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for bid, sold in ((0.38, False), (0.30, True)):  # 52% under: the global line, but this lane's is 60%; 62% under: past it
        store.db.execute("UPDATE decisions SET exit_ts=NULL, exit_price=NULL, close_time=?", (soon,))
        store.db.commit()
        notes = runner.stop_loss_pass(s, Client(bid), Store(s.db_path))
        assert bool(notes) is sold, bid


def test_a_lane_waits_after_a_sale_before_buying_the_same_market_again(setup):
    _s, store = setup
    store.db.execute("UPDATE decisions SET exit_ts=?, exit_price=0.50", (1000.0,))
    store.db.commit()
    assert store.exited_within("KXBTC15M-1", "fav", 90, now=1030.0) == pytest.approx(60.0)  # sold 30 s ago: 60 s still to wait
    assert store.exited_within("KXBTC15M-1", "fav", 90, now=1100.0) == 0.0  # sold 100 s ago: free
    assert store.exited_within("KXBTC15M-2", "fav", 90, now=1030.0) == 0.0  # another market is not held back
    assert store.exited_within("KXBTC15M-1", "other_lane", 90, now=1030.0) == 0.0  # nor is another lane


def test_evaluate_passes_during_the_cooldown_and_buys_after_it(tmp_path):
    import time as _t

    from tests.test_sizing import Est, settings, shot

    s = settings(tmp_path)
    (s.data_dir / "strategies.toml").write_text(
        "[lane.cool]\nestimator='llm'\nprice=[0.01,0.99]\nhours=[0.0,48.0]\nalways=true\nfixed_stake=2.0\nreentry_cooldown_seconds=90\n")
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time, exit_ts, exit_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (_t.time() - 300, "KXA-1", "bet", "observe", 0.5, "yes", "cool", 3, 1.6, "2099-01-01T00:00:00Z", _t.time() - 30, 0.4))
    store.db.commit()
    waiting = runner.evaluate(s, None, Est(0.5), store, shot(ticker="KXA-1", lane="cool", ask=0.5))
    assert waiting.action == "pass" and "cooling down" in waiting.note
    store.db.execute("UPDATE decisions SET exit_ts=?", (_t.time() - 200,))
    store.db.commit()
    free = runner.evaluate(s, None, Est(0.5), store, shot(ticker="KXA-1", lane="cool", ask=0.5))
    assert free.action == "bet"
