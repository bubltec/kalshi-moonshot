"""Sell into the first bounce: after the held bid has fallen bounce_fall under the price paid and bounced bounce_up off its low, the position is sold (scripts/exit_tradeoff.py)."""
import dataclasses

import pytest

from kalshi_moonshot import lanes
from tests.test_drop_exit import _held, _quote
from tests.test_settlement import _watcher

T = "KXCOPPER15M-1"
EXTRA = "bounce_fall=0.15\nbounce_up=0.04\n"


def _bid_path(w, now, bids):
    """One quote a second ending at `now`; a YES holder, so the quote's bid is the held side's bid (the bet was bought at 0.90)."""
    for k, b in enumerate(bids):
        _quote(w, T, b, now - (len(bids) - 1 - k))


def test_the_lane_setting_reaches_the_held_market(tmp_path, monkeypatch):
    _w, t, _close = _held(tmp_path, monkeypatch, drop=0, extra=EXTRA)
    assert t.bounce_fall == pytest.approx(0.15) and t.bounce_up == pytest.approx(0.04)


def test_a_fall_and_then_a_bounce_sells_at_the_bounce(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch, drop=0, extra=EXTRA)
    _bid_path(w, close - 150, [0.90, 0.88, 0.80, 0.72, 0.70, 0.70, 0.71])  # fell 0.20 under the 0.90 paid, a 1c wobble off the low: not yet
    assert w._jobs.empty()
    _quote(w, T, 0.75, close - 148)  # 5c off the 0.70 low
    assert not w._jobs.empty()
    assert w._jobs.get_nowait()[2].startswith("bounce: bid fell to 0.70 (paid 0.90) and bounced to 0.75")


def test_a_fall_that_has_not_bounced_is_held(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch, drop=0, extra=EXTRA)
    _bid_path(w, close - 150, [0.90, 0.85, 0.80, 0.74, 0.70, 0.66, 0.62])  # still sliding: nothing to sell into
    assert w._jobs.empty()


def test_a_small_fall_is_not_enough(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch, drop=0, extra=EXTRA)
    _bid_path(w, close - 150, [0.90, 0.84, 0.79, 0.84, 0.86])  # fell 11c under the price paid (the bar is 15c), then bounced 7c
    assert w._jobs.empty()


def test_a_bounce_in_the_last_ten_seconds_is_left_to_settle(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch, drop=0, extra=EXTRA)
    _bid_path(w, close - 8, [0.90, 0.80, 0.70, 0.70, 0.76])  # 8 s left: nothing left to catch
    assert w._jobs.empty()


def test_the_exit_is_off_unless_the_lane_sets_it(tmp_path, monkeypatch):
    w, _s, _store, close = _watcher(tmp_path, monkeypatch, ticker=T)
    t = w.targets[(T, "guarded")]
    assert t.bounce_fall == 0.0
    _bid_path(w, close - 150, [0.90, 0.80, 0.70, 0.70, 0.76])
    while not w._jobs.empty():
        assert not w._jobs.get_nowait()[2].startswith("bounce")


def test_a_lane_that_does_not_use_it_keeps_its_version(tmp_path, monkeypatch):
    _w, s, _store, _close = _watcher(tmp_path, monkeypatch)
    spec = lanes.spec_for(s, "guarded")
    assert "bounce_fall" not in lanes.settings_of(spec, s)
    assert lanes.settings_of(dataclasses.replace(spec, bounce_fall=0.15), s)["bounce_fall"] == "0.15"
