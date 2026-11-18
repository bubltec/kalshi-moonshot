"""The trail_timed step: a shadow-only sell step that asks the trail's question with the static typical minute and with the time-aware one (pricevol.py), and records both verdicts (2026-11-18, user: "so having a static volatility number is pointless"). Integration: lane file -> watcher -> signals and rows."""
import json
import logging

import pytest

from kalshi_moonshot import pricevol, sellchain, watcher
from kalshi_moonshot import ticks as ticks_mod
from tests.test_risk_exit_flow import TICKER, held, run_up, signals, tick


@pytest.fixture(autouse=True)
def _clean():
    ticks_mod.mids._rows.clear()
    yield
    ticks_mod.mids._rows.clear()


def rows(tmp_path):
    f = tmp_path / "risk_shadow.jsonl"
    return [x for x in map(json.loads, f.read_text().splitlines()) if x.get("kind") == "trail_timed"] if f.exists() else []


def lane(tmp_path, monkeypatch, chain="['trail_timed','profit_take','trail']", live_trail=True):
    return held(tmp_path, monkeypatch, live=True, take="true", extra=f"sell_chain={chain}\ncurve_take_trail=1.0\ncurve_take_trail_live={'true' if live_trail else 'false'}")


def test_unit_trail_timed_is_a_step_a_chain_may_name_and_not_in_the_default_order():
    assert "trail_timed" in sellchain.SELL_STEPS and "trail_timed" not in sellchain.DEFAULT and "trail_timed" not in sellchain.order_of(())
    assert callable(watcher._SELL["trail_timed"])


def test_integration_it_records_both_verdicts_both_units_and_never_sells(tmp_path, monkeypatch):
    w = lane(tmp_path, monkeypatch, chain="['trail_timed']")  # only the shadow step: nothing in the chain can sell
    assert run_up(w) is None
    assert tick(w, 0.89, 250) == [] and tick(w, 0.86, 249) == [] and tick(w, 0.85, 248) == []  # four cents off the high: the real trail would sell here
    got = rows(tmp_path)
    assert got, "the step wrote nothing"
    first = got[0]
    assert first["hit_static"] is True and first["ticker"] == TICKER and first["live"] is False and first["bid"] == 0.86 and first["high"] == 0.9 and first["units"] == 1.0
    assert first["unit_static"] > 0 and first["unit_timed"] > 0 and first["would_differ"] is (first["hit_static"] != first["hit_timed"])
    assert all(x["would_differ"] is (x["hit_static"] != x["hit_timed"]) for x in got)
    assert all((x["hit_static"], x["hit_timed"]) != (y["hit_static"], y["hit_timed"]) for x, y in zip(got, got[1:]))  # one row a change of verdict, not one a check


def test_integration_a_verdict_that_differs_is_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(pricevol, "implied_rate", lambda seq, left, **k: 0.05)  # a time-aware minute of 0.39: a four-cent fall is nothing
    w = lane(tmp_path, monkeypatch, chain="['trail_timed']")
    run_up(w)
    tick(w, 0.86, 250)
    first = rows(tmp_path)[0]
    assert first["hit_static"] is True and first["hit_timed"] is False and first["would_differ"] is True and first["unit_timed"] == pytest.approx(0.05 * 60**0.5, abs=1e-4)


def test_integration_the_step_does_not_change_the_trail_sale(tmp_path, monkeypatch):
    out = []
    for name, chain in (("alone", "['profit_take','trail']"), ("both", "['trail_timed','profit_take','trail']")):
        (tmp_path / name).mkdir()
        ticks_mod.mids._rows.clear()
        w = lane(tmp_path / name, monkeypatch, chain=chain)
        run_up(w)
        tick(w, 0.89, 250)
        out.append(tick(w, 0.86, 249))
    alone, both = out
    assert alone and both and both[0].why == alone[0].why.replace("profit_take > trail |", "trail_timed > profit_take > trail |")
    assert rows(tmp_path / "alone") == [] and rows(tmp_path / "both")


def test_integration_a_lane_that_does_not_name_it_writes_no_row(tmp_path, monkeypatch):
    w = lane(tmp_path, monkeypatch, chain="['profit_take','trail']")
    run_up(w)
    tick(w, 0.86, 250)
    assert rows(tmp_path) == []


def test_integration_with_the_trail_off_it_writes_nothing(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", extra="sell_chain=['trail_timed']")
    run_up(w)
    tick(w, 0.86, 250)
    assert rows(tmp_path) == []


def test_integration_an_error_in_the_time_aware_unit_is_logged_once_and_the_trail_still_sells(tmp_path, monkeypatch, caplog):
    def broken(*a, **k):
        raise RuntimeError("a bad grid")

    monkeypatch.setattr(pricevol, "implied_rate", broken)
    w = lane(tmp_path, monkeypatch)
    run_up(w)
    tick(w, 0.89, 250)
    with caplog.at_level(logging.WARNING, logger="moonshot"):
        out = tick(w, 0.86, 249)
        tick(w, 0.85, 248)
    assert out and out[0].why.startswith("curve risk exit (profit take): trail")  # the live trail sold as it always does
    assert rows(tmp_path)[0]["unit_timed"] is None
    assert len([r for r in caplog.records if "time-aware unit failed" in r.getMessage()]) == 1


def test_integration_an_error_in_the_step_itself_does_not_stop_the_trail(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(watcher.ProfitWatcher, "_risk_unit_timed", lambda *a, **k: 1 / 0)
    w = lane(tmp_path, monkeypatch)
    run_up(w)
    tick(w, 0.89, 250)
    with caplog.at_level(logging.WARNING, logger="moonshot"):
        out = tick(w, 0.86, 249)
    assert out and out[0].why.startswith("curve risk exit (profit take): trail") and rows(tmp_path) == []
    assert len([r for r in caplog.records if "trail_timed step failed" in r.getMessage()]) == 1
