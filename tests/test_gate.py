import time

import pytest

from kalshi_moonshot import config, gate, lanes
from kalshi_moonshot.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def bet(store, lane, minute_in, won, i, stake=1.0, close=100_000.0):
    opened = close - (15 - minute_in) * 60
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, outcome, close_time, settled_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (opened, f"KXBTC15M-{i}", "bet", "observe", 0.3, "yes", lane, 3, stake, "yes" if won else "no",
         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(close)), close))


def fill(store, lane, minute_in, wins, losses, start=0):
    for k in range(wins + losses):
        bet(store, lane, minute_in, k < wins, start + k)
    store.db.commit()


def test_a_band_needs_enough_bets_and_a_positive_lower_bound(store):
    fill(store, "late_ticket", 13.0, wins=18, losses=14, start=0)  # 32 bets: 18 win $2 (3 contracts) each, 14 lose
    fill(store, "late_ticket", 10.0, wins=6, losses=24, start=100)  # 30 bets, mostly losers
    fill(store, "late_ticket", 7.0, wins=3, losses=1, start=200)  # 4 bets: too few
    cells = gate.table(store, ("late_ticket",))
    assert cells[(12, 15)].n == 32 and cells[(9, 12)].n == 30 and cells[(6, 9)].n == 4
    assert gate.decide(cells[(12, 15)], 30, 1.0)[0]
    assert not gate.decide(cells[(9, 12)], 30, 1.0)[0]
    ok, why = gate.decide(cells[(6, 9)], 30, 1.0)
    assert not ok and "needs 30" in why


def test_only_the_named_lanes_feed_the_gate(store):
    fill(store, "late_ticket", 13.0, wins=20, losses=10)
    fill(store, "steady", 13.0, wins=0, losses=40, start=500)  # another lane's losses must not count
    assert gate.table(store, ("late_ticket",))[(12, 15)].n == 30


def test_a_lane_without_a_gate_is_always_allowed(store):
    spec = lanes.builtin_lanes(config.load())["commodity"]
    assert gate.check(store, spec, "2099-01-01T00:00:00Z") == (True, "")


def test_the_gate_is_looked_up_by_how_far_into_the_window_the_market_is(store):
    fill(store, "late_ticket", 13.0, wins=20, losses=10)
    toml = ("[lane.t]\nestimator='ladder'\nprice=[0.05,0.4]\nhours=[0.0,1.0]\ngate_from=['late_ticket']\n")
    spec = lanes.lane_specs_from(config.load(), toml)["t"]
    now = 1_000_000.0
    gate._CACHE.clear()
    close_late = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 120))  # 2 minutes left = 13 in
    close_early = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 600))  # 10 minutes left = 5 in
    assert gate.check(store, spec, close_late, now)[0]
    ok, why = gate.check(store, spec, close_early, now)
    assert not ok and "needs 30" in why
