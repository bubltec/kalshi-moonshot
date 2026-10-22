import pytest

from kalshi_moonshot import config, lanes, runner
from kalshi_moonshot.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def order(store, side, count, ticker="KXUSDJPY15M-1"):
    store.add_order(ticker=ticker, env="prod", count=count, price=0.05, fill_count=count, cost_usd=1.0, fee_usd=0.0,
                    order_id="x", side=side, contract="yes")


def decision(store, lane, exited):
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, exit_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1.0, "KXUSDJPY15M-1", "bet", "trade", 0.05, "yes", lane, 3, 0.15, 5.0 if exited else None))
    store.db.commit()


def test_a_second_buy_after_an_exit_is_still_a_held_position(store):
    order(store, "buy", 10)
    decision(store, "late_ticket_trail", exited=True)
    order(store, "sell", 10)
    assert store.open_position_tickers("prod") == set()  # flat: nothing held
    order(store, "buy", 18)  # a second lane buys the same market after that exit
    assert store.open_position_tickers("prod") == {"KXUSDJPY15M-1"}  # so a third buy is refused by the risk gate


def test_the_stop_loss_can_be_turned_off_per_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    toml = "[lane.ticket]\nestimator='ladder'\nprice=[0.05,0.4]\nhours=[0.0,1.0]\nstop_loss=false\n"
    spec = lanes.lane_specs_from(config.load(), toml)["ticket"]
    assert spec.stop_loss is False
    assert lanes.lane_specs_from(config.load(), None)["commodity"].stop_loss is True
    assert runner.lane_specs  # the pass reads each lane's flag
