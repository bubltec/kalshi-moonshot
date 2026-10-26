import pytest

from kalshi_moonshot import config, lanes
from kalshi_moonshot.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def bet(store, lane, ticker="KXBTC15M-1", ask=0.30):
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", (1.0, ticker, "bet", "observe", ask, "yes", lane, 3, 0.9, "2099-01-01T00:00:00Z"))
    store.db.commit()


def test_two_lanes_can_each_hold_the_same_market(store):
    bet(store, "late_ticket")
    assert store.holds_sized_bet("KXBTC15M-1", "late_ticket")
    assert not store.holds_sized_bet("KXBTC15M-1", "late_ticket_trail")  # the control lane may still bet it
    assert store.holds_sized_bet("KXBTC15M-1")  # with no lane named, any lane counts (as before)


def test_open_bets_are_listed_per_lane(store):
    bet(store, "late_ticket")
    bet(store, "late_ticket_trail")
    assert sorted(r["lane"] for r in store.open_bets()) == ["late_ticket", "late_ticket_trail"]


def test_an_exit_closes_only_its_own_lanes_bet(store):
    bet(store, "late_ticket")
    bet(store, "late_ticket_trail")
    store.mark_exited("KXBTC15M-1", 0.90, "take-profit", "late_ticket")
    assert [r["lane"] for r in store.open_bets()] == ["late_ticket_trail"]
    store.mark_exited("KXBTC15M-1", 0.10, "legacy call without a lane")
    assert store.open_bets() == []


def test_the_control_lane_is_flagged_to_share_markets():
    toml = "[lane.control]\nestimator = 'llm'\nprice = [0.5, 0.8]\nhours = [1, 72]\nshares_markets = true\n"
    spec = lanes.lane_specs_from(config.load(), toml)["control"]
    assert spec.shares_markets


def test_a_sale_saves_the_context_at_the_moment_of_the_sale(store):
    bet(store, "late_ticket")
    store.mark_exited("KXBTC15M-1", 0.60, "stop", "late_ticket", '{"bid": 0.6, "rise_30": -0.12}')
    row = store.db.execute("SELECT exit_factors, exit_price FROM decisions WHERE ticker='KXBTC15M-1'").fetchone()
    assert row["exit_factors"] == '{"bid": 0.6, "rise_30": -0.12}' and row["exit_price"] == 0.60
    bet(store, "late_ticket_trail")
    store.mark_exited("KXBTC15M-1", 0.90, "take-profit", "late_ticket_trail")  # a caller that saves no context leaves it empty
    assert store.db.execute("SELECT exit_factors FROM decisions WHERE lane='late_ticket_trail'").fetchone()["exit_factors"] is None
