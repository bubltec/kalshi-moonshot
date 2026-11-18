"""The shape_timed step: a shadow-only sell step that wants a bet to have stayed behind, with a flat or falling path, for a share of the time left (pathshape.timed_window; sell step `shape_timed`).

2026-11-18, user: "they should be time based where it waits or has to exhibit the behavior for a certain time relative to the time remaining". The `shape` step marks a bet the first moment it is behind with such a path; this one waits a tenth of the time left (the time left counted as no less than 90 s). Unit tests: the window; integration through a lane file and the watcher: no row before the wait, one row after it, a bet that comes back ahead starts over, a lane without the step writes none, an error in the step stops no sale."""
import logging

import pytest

from kalshi_moonshot import pathshape, sellchain, watcher
from kalshi_moonshot import ticks as ticks_mod
from tests.test_risk_exit_flow import TICKER, climb, held, level
from tests.test_shape_shadow import (  # noqa: F401  (the fixture and helpers of the shape step's own tests)
    FALL,
    SERIES,
    _clean,
    rows,
    run,
    seed,
)


def test_unit_the_wait_is_a_tenth_of_the_time_left_and_stops_shrinking_at_90_seconds():
    assert pathshape.timed_window(900.0) == pytest.approx(90.0)
    assert pathshape.timed_window(450.0) == pytest.approx(45.0)
    assert pathshape.timed_window(90.0) == pytest.approx(9.0)
    assert pathshape.timed_window(20.0) == pytest.approx(9.0) and pathshape.timed_window(-5.0) == pytest.approx(9.0)


def test_unit_shape_timed_is_a_step_a_chain_may_name_and_is_not_in_the_default_order():
    assert "shape_timed" in sellchain.SELL_STEPS and "shape_timed" not in sellchain.DEFAULT and "shape_timed" not in sellchain.order_of(())
    sellchain.check(("shape_timed", "profit_take"), "lane 'a'")
    assert callable(watcher._SELL["shape_timed"]) and "timed" in pathshape.LOOKS


def test_integration_a_steady_fall_gets_one_row_only_after_the_wait_and_no_sale(tmp_path, monkeypatch, caplog):
    seed(0.002)
    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['shape_timed']")
    with caplog.at_level(logging.INFO, logger="moonshot"):
        assert run(w, FALL[:30]) == []  # 30 s behind: under the 45 s a market with 449 s left asks
        assert rows(tmp_path, kind="shape_timed") == []
        assert run(w, FALL[30:], left=449 - 30) == []  # the rest of the fall: no sale from this step
    got = rows(tmp_path, kind="shape_timed")
    assert len(got) == 1  # one row a bet
    x = got[0]
    assert x["would_sell"] is True and x["live"] is False and x["lane"] == "risk_only" and x["ticker"] == TICKER
    assert x["behind_for"] >= x["window"] >= 40.0 and x["window"] == pytest.approx(pathshape.timed_window(x["left"]), abs=6.0) and x["share"] == pathshape.TIMED_SHARE
    assert x["buy"]["shape"] == "trend_against" and x["buy"]["window"] == pytest.approx(x["window"], abs=6.0) and x["quarter"]["shape"] in ("trend_against", "flat", "unknown")
    assert x["left"] < 449 - 30 and sum("would sell (shape_timed)" in r.getMessage() for r in caplog.records) == 1


def test_integration_a_bet_that_comes_back_ahead_starts_the_wait_over(tmp_path, monkeypatch):
    seed(0.002)
    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['shape_timed']")
    path = [0.80] * 30 + [0.90] * 5 + [0.80] * 30  # behind 30 s, ahead 5 s, behind 30 s: neither stretch reaches 45 s
    assert run(w, path) == [] and rows(tmp_path, kind="shape_timed") == []
    assert rows(tmp_path, kind="shape_timed") == [] and run(w, [0.80] * 40, left=449 - 65) == []
    assert len(rows(tmp_path, kind="shape_timed")) == 1  # the second stretch passed 45 s


def test_integration_a_lane_without_the_step_writes_no_shape_timed_row(tmp_path, monkeypatch):
    seed(0.002)
    w = held(tmp_path, monkeypatch, live=False, extra="sell_chain=['profit_take','trail','long_hold','loss']")
    run(w, FALL)
    assert rows(tmp_path, kind="shape_timed") == []


def test_integration_an_error_in_the_step_is_logged_once_a_bet_and_stops_nothing(tmp_path, monkeypatch, caplog):
    def broken(*a, **k):
        raise RuntimeError("a bad path")

    monkeypatch.setattr(watcher.ProfitWatcher, "_shape_timed_read", broken)
    seed(0.002)
    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['shape_timed']")
    with caplog.at_level(logging.WARNING, logger="moonshot"):
        assert run(w, FALL) == []  # the step raised on every check and the walk went on
    warned = [r.getMessage() for r in caplog.records if "the shape_timed step failed" in r.getMessage()]
    assert len(warned) == 1 and TICKER in warned[0] and "a bad path" in warned[0]


def test_integration_the_step_asked_beside_profit_take_does_not_change_its_sale(tmp_path, monkeypatch):
    out = []
    for name, chain in (("alone", "['profit_take']"), ("both", "['shape_timed','profit_take']")):
        (tmp_path / name).mkdir()
        ticks_mod.mids._rows.clear()
        w = held(tmp_path / name, monkeypatch, live=True, take="true", extra=f"sell_chain={chain}")
        assert climb(w, 0.82, 0.92, 700) is None
        out.append(level(w, 0.92, 700))
    alone, both = out
    assert alone is not None and both is not None and alone.why.startswith("curve risk exit (profit take)") and both.why.startswith("curve risk exit (profit take)")
