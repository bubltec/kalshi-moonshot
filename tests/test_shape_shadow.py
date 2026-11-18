"""The shape step: a shadow-only sell step that records the look-back shape of a losing bet (pathshape.py, sell step `shape`).

2026-11-18, user: "if the path is wavy it means there's likely a comeback, which will at bare minimum get us a better sale"; "if look back is trend or flat, and we're losing, we should probably exit though"; "we need to test these concepts". The step never sells: it writes rows (kind "shape") to data/risk_shadow.jsonl for scripts/shape_shadow_score.py to score forward. Unit tests: each shape from a hand-made path. Integration tests: lane file -> watcher -> its signals and its rows."""
import json
import logging
from collections import deque

import pytest

from kalshi_moonshot import pathshape, sellchain, watcher
from kalshi_moonshot import ticks as ticks_mod
from tests.test_risk_exit_flow import CLOSE, TICKER, climb, held, level, signals, yes_bid

SERIES = TICKER.split("-")[0]
UNIT = 0.02  # a market whose typical minute is two cents
# a series' hour of readings (net_z, eff, ground_z): its median path efficiency is 0.5, its median ground 2 typical moves, and its range is wide
HOUR = [(-30.0 + 0.5 * k, 0.05 + 0.9 * k / 119.0, (k / 30.0 if k < 60 else 2.0 + 5.0 * (k - 60))) for k in range(120)]


@pytest.fixture(autouse=True)
def _clean():
    ticks_mod.mids._rows.clear()
    pathshape.SEEN.clear()
    yield
    ticks_mod.mids._rows.clear()
    pathshape.SEEN.clear()


def path(bids, start=1000.0, step=1.0):
    return [(start + step * k, b) for k, b in enumerate(bids)]


def read(bids, seen=HOUR, unit=UNIT, start=1000.0):
    p = path(bids, start)
    return pathshape.shape(p, start, p[-1][0], unit, seen)


# ---- unit: each shape from a hand-made path ----
def test_unit_a_steady_fall_is_a_trend_against_the_bet():
    got = read([0.80 - 0.002 * k for k in range(61)])  # 12 cents down in a minute, in a market that typically moves 2
    assert got.shape == "trend_against" and got.direction == "against"
    assert got.net == pytest.approx(-0.12) and got.ground == pytest.approx(0.12) and got.eff == pytest.approx(1.0)
    assert got.reversals == 0 and got.quotes == 61 and got.typical == pytest.approx(0.02) and got.window == 60.0
    assert pathshape.would_sell(got.shape)


def test_unit_a_flat_line_is_flat():
    got = read([0.60] * 61)
    assert got.shape == "flat" and got.net == 0.0 and got.ground == 0.0 and got.reversals == 0 and got.direction == "none"
    assert pathshape.would_sell(got.shape)
    assert read([0.60 + (0.01 if k % 2 else 0.0) for k in range(61)]).shape == "flat"  # a one-cent flicker is not a wave: no leg reaches the market's typical move


def test_unit_a_zig_zag_that_ends_where_it_began_is_wavy():
    wave = [0.70, 0.68, 0.66, 0.64, 0.62, 0.60, 0.62, 0.64, 0.66, 0.68]
    got = read(wave * 6 + [0.70])  # six waves of ten cents, back at 0.70
    assert got.shape == "wavy" and got.net == pytest.approx(0.0) and got.ground == pytest.approx(1.2) and got.reversals == 11
    assert not pathshape.would_sell(got.shape)


def test_unit_a_fall_made_of_waves_is_wavy_not_a_trend():
    bids, b = [], 0.80
    for _ in range(6):  # down six cents, up four, six times: twelve cents lower, but far less straight than the series' median path
        for d in (-0.02, -0.02, -0.02, 0.02, 0.02):
            bids.append(round(b, 4))
            b += d
    got = read([*bids, round(b, 4)])
    assert abs(got.net) > got.typical and got.eff < 0.5 and got.reversals >= 2 and got.shape == "wavy"


def test_unit_a_rise_while_behind_is_a_trend_for_the_bet_never_a_would_sell():
    got = read([0.50 + 0.002 * k for k in range(61)])
    assert got.shape == "trend_for" and got.direction == "for" and not pathshape.would_sell(got.shape)


def test_unit_too_few_quotes_or_no_typical_minute_or_no_hour_of_readings_is_unknown():
    assert read([0.80, 0.78, 0.76]).shape == "unknown"  # three quotes
    assert read([0.80 - 0.002 * k for k in range(61)], unit=None).shape == "unknown"  # the market's typical minute cannot be read
    assert read([0.80 - 0.002 * k for k in range(61)], seen=HOUR[:5]).shape == "unknown"  # too few earlier readings to say what "most" is
    late = path([0.80 - 0.002 * k for k in range(20)], start=1040.0)  # the quotes begin two thirds of the way into the window
    assert pathshape.shape(late, 1000.0, 1060.0, UNIT, HOUR).shape == "unknown"
    assert not pathshape.would_sell("unknown")


def test_unit_the_price_standing_at_the_windows_start_is_its_first_point():
    p = path([0.80 - 0.002 * k for k in range(121)])  # two minutes of quotes; the window is the second minute
    got = pathshape.shape(p, 1060.0, 1120.0, UNIT, HOUR)
    assert got.shape == "trend_against" and got.net == pytest.approx(-0.12) and got.quotes == 61


def test_unit_a_reading_outside_the_series_own_range_is_an_anomaly_and_not_a_would_sell():
    got = read([0.95 - 0.015 * k for k in range(61)])  # ninety cents in a minute: 45 typical moves, where the hour's readings stop at 30
    assert got.shape == "anomaly" and got.net_z < -30 and not pathshape.would_sell(got.shape)
    assert read([0.95 - 0.015 * k for k in range(61)], seen=HOUR[:50]).shape == "trend_against"  # under 100 readings there is no range to be outside of


def test_unit_most_is_the_series_own_median_not_a_fixed_number():
    bids, b = [], 0.80
    for _ in range(6):
        for d in (-0.02, -0.02, -0.02, 0.02, 0.02):
            bids.append(round(b, 4))
            b += d
    choppy_hour = [(z, e * 0.2, g) for z, e, g in HOUR]  # an hour whose paths were all far from straight: median efficiency 0.1
    assert read(bids).shape == "wavy" and read(bids, seen=choppy_hour).shape == "trend_against"  # the same path is the straight one there


def test_unit_reversals_count_turns_between_legs_of_the_given_size():
    assert pathshape.reversals([0.5, 0.6, 0.5, 0.6, 0.5], 0.05) == 3
    assert pathshape.reversals([0.5, 0.6, 0.5, 0.6, 0.5], 0.2) == 0
    assert pathshape.reversals([0.5, 0.51, 0.5, 0.4, 0.41, 0.3], 0.05) == 0  # one fall with two one-cent pauses


def test_unit_the_series_hour_drops_readings_older_than_an_hour():
    pathshape.SEEN[("S", "buy")] = deque([(100.0, 1.0, 0.5, 2.0), (4000.0, -1.0, 0.4, 3.0)])
    assert pathshape.seen_of("S", "buy", 4100.0) == [(-1.0, 0.4, 3.0)]
    pathshape.note("S", "buy", 4100.0, pathshape.Shape("unknown"))  # no typical move: not a reading
    assert len(pathshape.SEEN[("S", "buy")]) == 1


def test_unit_shape_is_a_step_a_chain_may_name_and_is_not_in_the_default_order():
    assert "shape" in sellchain.SELL_STEPS and "shape" not in sellchain.DEFAULT and "shape" not in sellchain.order_of(())
    sellchain.check(("shape", "profit_take"), "lane 'a'")
    assert callable(watcher._SELL["shape"])


# ---- integration: lane file -> watcher -> signals and rows ----
def seed(step):
    """The market before the buy: 450 s of its own quotes, one a second, moving `step` up and down (its typical minute is step * sqrt(60)); and its series' hour of shape readings."""
    mid = 0.80
    for k, left in enumerate(range(900, 450, -1)):
        ticks_mod.mids.note(TICKER, CLOSE - left, mid - 0.005, mid + 0.005)
        mid += step if k % 2 == 0 else -step
    for look in pathshape.LOOKS:
        pathshape.SEEN[(SERIES, look)] = deque((CLOSE - 460.0, *r) for r in HOUR)


def run(w, bids, left=449):
    """One quote a second, each also in the market's own quote history; every sale signal raised on the way."""
    out = []
    for k, b in enumerate(bids):
        ticks_mod.mids.note(TICKER, CLOSE - left + k, b - 0.005, b + 0.005)
        yes_bid(w, b, left - k)
        out += signals(w)
    return out


def rows(tmp_path, kind="shape"):
    f = tmp_path / "risk_shadow.jsonl"
    return [x for x in map(json.loads, f.read_text().splitlines()) if x.get("kind") == kind] if f.exists() else []


FALL = [round(0.80 - 0.002 * k, 4) for k in range(200)]  # the fixture bet paid 0.81: behind from the first quote, and falling steadily
WAVES = [0.86, 0.88, 0.90, 0.88, 0.86, 0.84, 0.86, 0.88, 0.90] + [round(0.90 - 0.02 * abs(((k + 6) % 12) - 6), 4) for k in range(1, 150)]  # ahead through two turns, then waves of twelve cents (0.90 to 0.78) that take it behind and back


def test_integration_a_losing_bet_on_a_steady_fall_gets_a_would_sell_row_and_no_sale(tmp_path, monkeypatch, caplog):
    seed(0.002)
    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['shape']")
    with caplog.at_level(logging.INFO, logger="moonshot"):
        assert run(w, FALL) == []  # twenty cents down and this step raised no sale
    got = rows(tmp_path)
    pairs = [(x["buy"]["shape"], x["quarter"]["shape"]) for x in got]
    assert pairs[0] == ("unknown", "unknown") and got[0]["would_sell"] is False  # the first second after the buy: too few quotes to judge, written as such
    assert len(pairs) == len(set(pairs)) == 3  # one row for each pair of shapes, not one a check
    got = got[1:]
    pairs = pairs[1:]
    assert all(x["would_sell"] is True and x["buy"]["shape"] == "trend_against" and x["buy"]["direction"] == "against" for x in got)
    first = got[0]
    assert first["live"] is False and first["pnl"] < 0 and first["bid"] < 0.81 and first["left"] > 400 and first["ticker"] == TICKER and first["lane"] == "risk_only"
    assert set(first["buy"]) == {"shape", "direction", "net", "ground", "eff", "reversals", "quotes", "typical", "window"} and first["buy"]["net"] < 0 and first["buy"]["eff"] == 1.0
    assert pairs[0] == ("trend_against", "unknown") and ("trend_against", "trend_against") in pairs  # the quarter look-back reaches back before the watcher's quotes at first, then fills
    assert sum("would sell (shape)" in r.getMessage() for r in caplog.records) == 1  # one line a bet


def test_integration_a_zig_zag_gets_rows_with_would_sell_false(tmp_path, monkeypatch, caplog):
    seed(0.01)
    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['shape']")
    with caplog.at_level(logging.INFO, logger="moonshot"):
        assert run(w, WAVES) == []
    got = rows(tmp_path)
    assert got and all(x["buy"]["shape"] == "wavy" and x["would_sell"] is False and x["pnl"] < 0 and x["buy"]["reversals"] >= 2 for x in got)
    assert not any("would sell (shape)" in r.getMessage() for r in caplog.records)


def test_integration_a_bet_that_is_ahead_gets_no_row(tmp_path, monkeypatch):
    seed(0.002)
    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['shape']")
    assert run(w, [round(0.86 + 0.001 * k, 4) for k in range(60)]) == [] and rows(tmp_path) == []
    assert len(pathshape.SEEN[(SERIES, "buy")]) > len(HOUR)  # its readings still went into the series' hour


def test_integration_a_lane_without_shape_in_its_chain_writes_no_shape_row(tmp_path, monkeypatch):
    for name, extra in (("plain", ""), ("chain", "sell_chain=['profit_take','trail','long_hold','loss']")):
        (tmp_path / name).mkdir()
        ticks_mod.mids._rows.clear()
        seed(0.002)
        w = held(tmp_path / name, monkeypatch, live=False, extra=extra)
        run(w, FALL)
        assert rows(tmp_path / name) == [] and len(w.targets[(TICKER, "risk_only")].shape_path) == 0
        assert rows(tmp_path / name, kind=None)  # the loss step's own shadow row is there as before: only the shape rows are absent


def test_integration_shape_first_in_a_chain_does_not_change_the_loss_steps_sale(tmp_path, monkeypatch):
    out = []
    for name, chain in (("loss", "['loss']"), ("both", "['shape','loss']")):
        (tmp_path / name).mkdir()
        ticks_mod.mids._rows.clear()
        seed(0.002)
        w = held(tmp_path / name, monkeypatch, live=True, extra=f"sell_chain={chain}")
        sale = None
        for k, b in enumerate(FALL):
            ticks_mod.mids.note(TICKER, CLOSE - 449 + k, b - 0.005, b + 0.005)
            yes_bid(w, b, 449 - k)
            got = signals(w)
            if got:
                sale = (k, got[0].why)
                break
        out.append(sale)
    alone, both = out
    assert alone is not None and both is not None and both[0] == alone[0]  # the same second
    assert alone[1].endswith("[sell chain lane: loss | sold at loss]") and both[1].endswith("[sell chain lane: shape > loss | sold at loss]")
    assert both[1].split(" [sell chain")[0] == alone[1].split(" [sell chain")[0]
    assert rows(tmp_path / "both") and rows(tmp_path / "loss") == []


def test_integration_an_exit_modules_steps_may_name_shape(tmp_path, monkeypatch):
    seed(0.002)
    w = held(tmp_path, monkeypatch, live=True, extra="exit_module='rt'\n\n[exit.rt]\nsteps=['shape','profit_take','trail']\n[exit.rt.profit]\n[exit.rt.loss]")
    assert w.targets[(TICKER, "risk_only")].sell_chain == ("shape", "profit_take", "trail") and w.targets[(TICKER, "risk_only")].bought_ts == CLOSE - 450
    assert run(w, FALL) == [] and rows(tmp_path)


def test_integration_an_error_in_the_shape_step_does_not_stop_the_profit_take(tmp_path, monkeypatch, caplog):
    """A shadow step must not be able to stop a sale: with the shape reading raising on every check, shape first in a chain takes the gain exactly as the profit take alone does."""
    def broken(*a, **k):
        raise RuntimeError("a bad path")

    monkeypatch.setattr(pathshape, "shape", broken)
    out = []
    with caplog.at_level(logging.WARNING, logger="moonshot"):
        for name, chain in (("alone", "['profit_take']"), ("both", "['shape','profit_take']")):
            (tmp_path / name).mkdir()
            ticks_mod.mids._rows.clear()
            w = held(tmp_path / name, monkeypatch, live=True, take="true", extra=f"sell_chain={chain}")
            assert climb(w, 0.82, 0.92, 700) is None
            out.append(level(w, 0.92, 700))
    alone, both = out
    assert alone is not None and both is not None and alone.why.startswith("curve risk exit (profit take)")
    assert both.why == alone.why.replace("[sell chain lane: profit_take | sold at profit_take]", "[sell chain lane: shape > profit_take | sold at profit_take]")
    assert rows(tmp_path / "both") == []  # no shape row
    warned = [r.getMessage() for r in caplog.records if "the shape step failed" in r.getMessage()]
    assert len(warned) == 1 and TICKER in warned[0] and "risk_only" in warned[0] and "a bad path" in warned[0]  # once a bet, not once a check


def test_integration_an_error_in_the_shape_step_does_not_stop_a_loss_sale_either(tmp_path, monkeypatch):
    monkeypatch.setattr(pathshape, "shape", lambda *a, **k: 1 / 0)
    seed(0.002)
    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['shape','loss']")
    got = run(w, FALL)
    assert got and got[0].why.startswith("curve risk exit (max loss)") and rows(tmp_path) == []
