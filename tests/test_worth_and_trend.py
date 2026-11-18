"""Worth and trend for both sides on one line, shadow (strategy.agreed_pick, strategy.agreed_tag): 2026-11-17, user, on KXBTC15M 18:45 (NO rated worth 0.72 for 0.62 by the ladder while NO's own risk had worsened two typical moves): "it never should have suggested that, at that time"; "Build the note". Unit tests on the pick; integration through a lane file and runner.evaluate with quotes and perp ticks in memory."""
import time

import pytest

from kalshi_moonshot import config, lanes, regime, runner, ticks
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot, agreed_pick, agreed_tag, both_sides
from tests.test_both_sides import LANE
from tests.test_risk_trend import FALLING, RISING, perp, quotes, setup  # noqa: F401
from tests.test_flip_rule import seed

TREND = "entry_risk_trend=0.25"


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.mids.clear()
    ticks.history.clear()
    runner._risk_marks.clear()
    runner._risk_seen.clear()
    runner._risk_seen_at.clear()


def spec(tmp_path, monkeypatch, extra=TREND):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra, sides="['yes','no']", needs="true"))
    s = config.load()
    return s, lanes.spec_for(s, "bs")


def test_unit_the_pick_needs_the_edge_rule_and_the_picked_sides_trend_to_agree(tmp_path, monkeypatch):
    _, sp = spec(tmp_path, monkeypatch)
    b = both_sides(sp, 0.60, 0.80, 0.83)  # the edge rule buys NO
    assert b.pick == "no"
    assert agreed_pick(b, {"yes": "worsening", "no": "holding"}) == "no"
    assert agreed_pick(b, {"yes": "holding", "no": "worsening"}) is None  # the BTC 18:45 case: worth says NO, the trend says YES
    assert agreed_pick(b, {"yes": "holding", "no": "anomaly"}) is None
    assert agreed_pick(b, {"yes": "holding", "no": None}) is None  # no reading is not agreement
    neither = both_sides(sp, 0.815, 0.80, 0.83)
    assert agreed_pick(neither, {"yes": "holding", "no": "holding"}) is None  # a good trend buys nothing the edge rule does not


def test_unit_the_tag_carries_each_sides_edge_and_trend_and_the_pick(tmp_path, monkeypatch):
    _, sp = spec(tmp_path, monkeypatch)
    b = both_sides(sp, 0.60, 0.80, 0.83)
    tag = agreed_tag(b, {"yes": (1.4, 116.0, "holding"), "no": (-2.0, 116.0, "worsening")})
    assert tag == f"[worth and trend: YES edge {b.yes.edge:+.3f}, risk trend +1.4 holding | NO edge {b.no.edge:+.3f}, risk trend -2.0 worsening | agree on neither]"
    assert agreed_tag(b, {"yes": (-1.0, 116.0, "worsening"), "no": (0.7, 116.0, "holding")}).endswith("agree on NO]")
    assert "YES edge" in agreed_tag(b, {"yes": None, "no": None}) and "risk trend unreadable" in agreed_tag(b, {"yes": None, "no": None})


class Fixed:
    def __init__(self, p):
        self.p = p

    def estimate(self, client, settings, shot):
        return Estimate(self.p, "fixed")


def decide(tmp_path, monkeypatch, now, p_yes, side="yes", extra=TREND, left=600.0):
    s, _ = spec(tmp_path, monkeypatch, extra)
    store = Store(s.db_path)
    regime.reset()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + left, 100.0, now))
    store.db.commit()
    bid, ask = (0.61, 0.62) if side == "yes" else (0.38, 0.39)  # one market: the quotes seeded below end at a YES mid of 0.61
    shot = LongShot("KXBTC15M-7", "t", "", bid, ask, left / 3600.0, "2099-01-01T00:00:00Z", left / 3600.0, lane="bs", series="KXBTC15M", side=side)
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed(p_yes)})


def tag_of(note):
    i = note.index("[worth and trend:")
    return note[i : note.index("]", i) + 1]


def test_integration_worth_and_trend_agree_on_the_side_that_is_rising_and_worth_more(tmp_path, monkeypatch):
    now = setup(RISING)  # YES climbing, the perp over its open
    out = decide(tmp_path, monkeypatch, now, 0.90)  # the model has YES worth 0.90 for 0.62
    assert out.action == "bet" and tag_of(out.note).endswith("agree on YES]") and "holding" in tag_of(out.note)


def test_integration_the_btc_case_worth_says_one_side_and_the_trend_the_other(tmp_path, monkeypatch):
    now = setup(RISING)  # the market is moving to YES
    out = decide(tmp_path, monkeypatch, now, 0.25, side="no")  # the model has NO worth 0.75 for 0.39: the edge rule buys NO
    tag = tag_of(out.note)
    assert "the rule buys NO]" in out.note and tag.endswith("agree on neither]")
    assert "NO edge +" in tag and "worsening" in tag.split("|")[1]  # NO is worth more than the bet and its risk is worsening
    assert out.action == "bet"  # shadow: the note says so and the buy still goes in


def test_integration_the_line_reads_the_same_from_either_side(tmp_path, monkeypatch):
    now = setup(FALLING, after=lambda k: -0.5)  # YES falling, the perp under its open: NO's risk is improving
    from_yes = decide(tmp_path, monkeypatch, now, 0.25, side="yes")
    from_no = decide(tmp_path, monkeypatch, now, 0.25, side="no")
    assert tag_of(from_yes.note) == tag_of(from_no.note) and tag_of(from_no.note).endswith("agree on NO]")


def test_integration_a_market_too_young_for_a_trend_agrees_on_neither(tmp_path, monkeypatch):
    now = time.time()
    opened = now - 100.0
    seed(quotes(now, opened, RISING))
    perp(now, opened, lambda k: 0.5)
    out = decide(tmp_path, monkeypatch, now, 0.90, left=800.0)
    assert "risk trend unreadable" in tag_of(out.note) and tag_of(out.note).endswith("agree on neither]")


def test_integration_no_line_without_the_risk_trend_and_no_decision_changes_with_it(tmp_path, monkeypatch):
    for level, p, side in ((RISING, 0.90, "yes"), (RISING, 0.25, "no"), (FALLING, 0.25, "yes")):
        now = setup(level)
        with_it = decide(tmp_path, monkeypatch, now, p, side=side)
        without = decide(tmp_path, monkeypatch, now, p, side=side, extra="")
        assert "[worth and trend:" in with_it.note and "[worth and trend:" not in without.note
        assert with_it.action == without.action


def test_integration_the_second_look_at_the_other_side_is_not_added_to_the_hours_range(tmp_path, monkeypatch):
    now = setup(RISING)
    decide(tmp_path, monkeypatch, now, 0.90, side="yes")
    assert len(runner._risk_seen["KXBTC15M"]) == 1  # the asked side's reading only, as before this note existed
