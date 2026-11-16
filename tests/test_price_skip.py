"""The no-buy range, a hole in a lane's price band (lane field price_skip). 2026-11-16, user: "a hard stop on buying from 42-58 while we figure out a better solution". A stopgap in fixed cents, the user's own numbers: no decision buys inside it, no bid rests inside it and no order is allowed to fill inside it."""
import dataclasses
import time

import pytest

from kalshi_moonshot import config, lanes, regime, runner, ticks, variants
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

from tests.test_buy_retry import Client, RestClient, setup  # noqa: F401  (the same exchange stand-ins and the same placing fixture: a YES side decided at an ask of 0.31 in a 0.02 to 0.39 band)

LANE = "[lane.mid]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nneeds_model_edge=false\nfixed_stake=1.0\nseries=['KXBTC15M']\n{extra}\n"


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.history.clear()
    variants._broad_memo = (0, None)


def decide(tmp_path, monkeypatch, ask, extra):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    now = time.time()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", round(ask - 0.01, 2), ask, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="mid", series="KXBTC15M", side="yes")
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})


# ---- the rule itself ----
def test_unit_the_range_includes_both_ends_and_is_off_by_default():
    base = lanes.LaneSpec(name="x", estimator="market", label="x", price=(0.1, 0.9), hours=(0.0, 1.0), max_spread=0.05, min_volume=0.0)
    assert not any(lanes.no_buy(base, p) for p in (0.0, 0.42, 0.50, 0.58))  # off: (0, 0)
    sp = dataclasses.replace(base, price_skip=(0.42, 0.58))
    assert all(lanes.no_buy(sp, p) for p in (0.42, 0.50, 0.58))
    assert not any(lanes.no_buy(sp, p) for p in (0.41, 0.59, 0.90))
    assert not lanes.no_buy(sp, None) and not lanes.no_buy(None, 0.50)


def test_unit_the_lane_file_reads_the_range_and_it_changes_the_version_only_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seen = {}
    for extra in ("", "price_skip=[0.42,0.58]"):
        (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
        s = config.load()
        sp = lanes.lane_specs(s)["mid"]
        seen[extra] = (sp.price_skip, "price_skip" in lanes.settings_of(sp, s), lanes.version_of(sp, s))
    assert seen[""][:2] == ((0.0, 0.0), False) and seen["price_skip=[0.42,0.58]"][:2] == ((0.42, 0.58), True)
    assert seen[""][2] != seen["price_skip=[0.42,0.58]"][2]


# ---- the decision: lane file -> evaluate ----
def test_integration_a_side_priced_inside_the_range_is_not_bought_and_the_note_says_why(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, 0.51, "price_skip=[0.42,0.58]")
    assert out.action == "pass" and "no-buy range: the yes ask 0.51 is inside the lane's 0.42 to 0.58" in out.note


def test_integration_the_edges_are_inside_and_a_cent_outside_is_bought(tmp_path, monkeypatch):
    assert decide(tmp_path, monkeypatch, 0.58, "price_skip=[0.42,0.58]").action == "pass"
    assert decide(tmp_path, monkeypatch, 0.42, "price_skip=[0.42,0.58]").action == "pass"
    assert decide(tmp_path, monkeypatch, 0.59, "price_skip=[0.42,0.58]").action == "bet"
    assert decide(tmp_path, monkeypatch, 0.41, "price_skip=[0.42,0.58]").action == "bet"


def test_integration_without_the_field_the_middle_is_bought_as_before(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, 0.51, "")
    assert out.action == "bet" and "no-buy range" not in out.note


# ---- the order: nothing fills inside the range even when the decision was made outside it ----
def test_integration_an_ask_that_moved_into_the_range_while_the_order_waited_is_not_bought(setup):
    s, store, shot, spec = setup
    spec(price_skip=(0.33, 0.36), buy_slip=0.06)
    client = Client(fills=[1.5], asks=[0.34])  # decided at 0.31; the book now asks 0.34
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 0 and client.sent == [] and "inside the lane's no-buy range 0.33 to 0.36" in got.note


def test_integration_an_order_decided_under_the_range_does_not_pay_its_way_up_into_it(setup):
    s, store, shot, spec = setup
    spec(price_skip=(0.32, 0.36), buy_slip=0.02)  # decided at 0.31: the limit would be 0.33, inside the range
    client = Client(fills=[1.5], asks=[0.30])
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 1.5 and client.sent == [(1.5, 0.31)]  # held a cent under the range


def test_integration_without_the_range_the_same_order_keeps_its_slip(setup):
    s, store, shot, spec = setup
    spec(buy_slip=0.02)
    client = Client(fills=[1.5], asks=[0.30])
    runner.place(s, client, store, shot, "why", 1.5)
    assert client.sent == [(1.5, 0.33)]


def test_integration_a_second_try_is_not_sent_into_the_range(setup):
    s, store, shot, spec = setup
    spec(price_skip=(0.33, 0.36), buy_retry_chase=0.06)
    client = Client(fills=[0.0, 1.5], asks=[0.30, 0.34])  # the first try fills nothing; by the second the ask is 0.34
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 0 and len(client.sent) == 1


def test_integration_a_bid_is_not_rested_inside_the_range_and_nothing_is_taken_instead(setup, monkeypatch):
    s, store, shot, spec = setup
    spec(buy_rest_seconds=5.0, buy_rest_then_take=True, buy_retries=0, price_skip=(0.28, 0.295))  # the book's ask 0.30 is outside; the bid a cent under it, 0.29, is not
    monkeypatch.setattr(runner.time, "sleep", lambda x: None)
    client = RestClient(fills=[1.5], asks=[0.30])
    got = runner.place(s, client, store, shot, "why", 1.5, order_key="k")
    assert client.rested == [] and client.sent == [] and got.filled == 0 and "no-buy range" in got.note


def test_integration_a_floor_bid_inside_the_range_is_not_placed(setup, monkeypatch):
    s, store, shot, spec = setup
    spec(price_skip=(0.28, 0.32))
    client = RestClient(fills=[1.5], asks=[0.35])
    got = runner.place(s, client, store, shot, "why", 1.5, rest_seconds=5.0)
    assert client.rested == [] and got.filled == 0 and "no-buy range" in got.note
