"""Both sides of a market judged together (strategy.both_sides): 2026-11-17, user: "we need a function that determines both sides at the same time", after KXHYPE15M 17:30, whose YES side was dropped at the screen while only its NO side reached the decisions log. Unit tests on the function; integration through a lane file into runner.evaluate and the ladder screen."""
from datetime import UTC, datetime

import pytest

from kalshi_moonshot import config, lanes, regime, runner, ticks
from kalshi_moonshot.estimators import Estimate, LadderEstimator
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot, both_sides, both_sides_tag, fee_per_contract

LANE = "[lane.bs]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nneeds_model_edge={needs}\nmin_edge_room=0.12\nsides={sides}\nfixed_stake=1.0\nseries=['KXBTC15M']\n{extra}\n"


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.mids.clear()
    ticks.history.clear()


def spec(tmp_path, monkeypatch, extra="", sides="['yes','no']", needs="true"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra, sides=sides, needs=needs))
    s = config.load()
    return s, lanes.spec_for(s, "bs")


def test_unit_the_hype_market_of_17_23_reads_as_the_screen_logged_it(tmp_path, monkeypatch):
    _, sp = spec(tmp_path, monkeypatch)
    b = both_sides(sp, 0.72, 0.80, 0.83)  # the model had YES at 72%; YES asked 0.83 and NO 0.20
    assert b.yes.worth == pytest.approx(0.72) and b.no.worth == pytest.approx(0.28)
    assert b.no.ask == pytest.approx(0.20) and b.no.cost == pytest.approx(0.20 + fee_per_contract(0.20))
    assert b.yes.edge == pytest.approx(-0.121, abs=0.002)  # the screen's -12.1%
    assert b.no.edge == pytest.approx(0.070, abs=0.002)  # the screen's +7.0%
    assert b.no.needs == pytest.approx(0.12 * (0.72 + b.no.edge))  # 12% of the room to a sure win
    assert not b.yes.clears and not b.no.clears and b.pick is None  # NO is worth more than the bet, by less than the lane asks


def test_unit_the_side_worth_enough_more_than_the_bet_is_the_pick(tmp_path, monkeypatch):
    _, sp = spec(tmp_path, monkeypatch)
    assert both_sides(sp, 0.60, 0.80, 0.83).pick == "no"  # NO worth 0.40 for 0.20
    assert both_sides(sp, 0.95, 0.80, 0.83).pick == "yes"  # YES worth 0.95 for 0.83
    assert both_sides(sp, 0.815, 0.80, 0.83).pick is None  # the model agrees with the market: the spread and the fee leave nothing


def test_unit_at_most_one_side_is_ever_worth_more_than_the_bet(tmp_path, monkeypatch):
    _, sp = spec(tmp_path, monkeypatch)
    for p in (0.02, 0.2, 0.41, 0.5, 0.63, 0.9, 0.99):
        for bid, ask in ((0.10, 0.11), (0.40, 0.44), (0.50, 0.51), (0.77, 0.80), (0.93, 0.94)):
            b = both_sides(sp, p, bid, ask)
            assert b.yes.edge + b.no.edge == pytest.approx(-(ask - bid) - fee_per_contract(ask) - fee_per_contract(1.0 - bid))
            assert not (b.yes.edge > 0 and b.no.edge > 0)
            assert not (b.yes.clears and b.no.clears)


def test_unit_a_side_the_lane_may_not_buy_is_never_the_pick(tmp_path, monkeypatch):
    _, capped = spec(tmp_path, monkeypatch, "")
    b = both_sides(capped, 0.999, 0.955, 0.96)  # YES clears the bar at a price over the lane's 0.95
    assert b.yes.clears and not b.yes.buyable and b.pick is None
    _, skip = spec(tmp_path, monkeypatch, "price_skip=[0.45,0.57]")
    b = both_sides(skip, 0.80, 0.49, 0.50)  # YES at 0.50 is in the no-buy range
    assert b.yes.clears and not b.yes.buyable and b.pick is None
    _, yes_only = spec(tmp_path, monkeypatch, sides="['yes']")
    b = both_sides(yes_only, 0.60, 0.80, 0.83)
    assert b.no.clears and not b.no.buyable and b.pick is None  # the lane does not buy NO


def test_unit_a_side_with_no_quote_is_not_buyable(tmp_path, monkeypatch):
    _, sp = spec(tmp_path, monkeypatch)
    b = both_sides(sp, 0.5, 0.0, 0.40)  # no YES bid: NO has no ask
    assert not b.no.buyable and not b.no.clears and b.pick == "yes"


def test_unit_the_tag_names_both_sides_and_the_pick(tmp_path, monkeypatch):
    _, sp = spec(tmp_path, monkeypatch)
    tag = both_sides_tag(both_sides(sp, 0.72, 0.80, 0.83))
    assert tag.startswith("[both sides: YES worth 0.72 costs 0.84") and "| NO worth 0.28 costs 0.21" in tag and tag.endswith("the rule buys neither]")
    assert both_sides_tag(both_sides(sp, 0.60, 0.80, 0.83)).endswith("the rule buys NO]")


class Fixed:
    def __init__(self, p):
        self.p = p

    def estimate(self, client, settings, shot):
        return Estimate(self.p, "fixed")


def decide(tmp_path, monkeypatch, p_yes, side="yes", needs="true"):
    import time

    s, _ = spec(tmp_path, monkeypatch, needs=needs)
    store = Store(s.db_path)
    regime.reset()
    now = time.time()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 600.0, 100.0, now))
    store.db.commit()
    bid, ask = (0.80, 0.83) if side == "yes" else (0.17, 0.20)  # one market: YES 0.80 / 0.83, NO 0.17 / 0.20
    shot = LongShot("KXBTC15M-7", "t", "", bid, ask, 600.0 / 3600.0, "2099-01-01T00:00:00Z", 600.0 / 3600.0, lane="bs", series="KXBTC15M", side=side)
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed(p_yes)})


def test_integration_a_pass_on_one_side_says_what_the_rule_made_of_the_other(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, 0.60, side="yes")  # asked about YES, which is worth less than it costs; NO is the side worth buying
    assert out.action == "pass" and "[both sides: YES worth 0.60" in out.note and "the rule buys NO]" in out.note


def test_integration_the_same_market_reads_the_same_from_either_side(tmp_path, monkeypatch):
    from_yes = decide(tmp_path, monkeypatch, 0.60, side="yes").note
    from_no = decide(tmp_path, monkeypatch, 0.60, side="no").note
    tag = lambda note: note[note.index("[both sides:") : note.index("]", note.index("[both sides:")) + 1]  # noqa: E731
    assert tag(from_yes) == tag(from_no)


def test_integration_the_pick_is_the_side_the_lane_buys(tmp_path, monkeypatch):
    for p_yes, picked in ((0.60, "no"), (0.95, "yes"), (0.815, None)):
        for side in ("yes", "no"):
            out = decide(tmp_path, monkeypatch, p_yes, side=side)
            assert (out.action == "bet") == (picked == side), (p_yes, side, out.note)
            assert ("the rule buys neither]" in out.note) == (picked is None)


def test_integration_a_lane_that_asks_for_no_edge_carries_no_tag(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, 0.60, needs="false")
    assert "[both sides:" not in out.note


class Pricer:
    def price_markets(self, rows, s, now):
        return {r["ticker"]: (0.72, "priced") for r in rows}


def test_integration_a_side_dropped_at_the_screen_is_logged_with_both_sides(tmp_path, monkeypatch):
    s, sp = spec(tmp_path, monkeypatch)
    est = LadderEstimator(Pricer())
    shots = [LongShot("KXBTC15M-7", "t", "", b, a, 0.1, "2099-01-01T00:00:00Z", 0.1, lane="bs", series="KXBTC15M", side=side, event_ticker="KXBTC15M-7") for side, b, a in (("yes", 0.80, 0.83), ("no", 0.17, 0.20))]
    kept = est.screen(shots, [{"ticker": "KXBTC15M-7", "event_ticker": "KXBTC15M-7", "series_ticker": "KXBTC15M"}], sp, s, datetime.now(UTC))
    assert kept == [] and {d["side"] for d in est.dropped} == {"yes", "no"}
    for d in est.dropped:
        assert "[both sides: YES worth 0.72" in d["reason"] and "NO worth 0.28" in d["reason"] and "the rule buys neither]" in d["reason"]
