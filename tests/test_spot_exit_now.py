import time
from types import SimpleNamespace

from kalshi_moonshot import variants


def _spec(**kw):
    base = {"shock_last": 300.0, "shock_units": 1.0, "shock_margin_units": 0.6, "shock_seconds": 60.0, "shock_memory": 120.0, "cross_units": -1.0}
    base.update(kw)
    return SimpleNamespace(**base)


def _ticks(monkeypatch, now, values, unit=0.0002):
    ts = [now - len(values) + 1 + i for i in range(len(values))]
    monkeypatch.setattr(variants.ticks_mod.history, "window", lambda index, a, b: (ts, values))
    monkeypatch.setattr(variants, "unit_index", lambda index, t=None, span=900.0: unit)


def test_a_spot_already_behind_the_strike_is_sold_at_once_by_the_cross_exit(monkeypatch):
    now = time.time()
    _ticks(monkeypatch, now, [100.0] * 200)
    spec = _spec(shock_units=0.0, cross_units=0.0)
    assert "cross exit" in variants.spot_exit_now(spec, "KXBTC15M", "yes", 200.0, 100.05, now)  # spot 5 bp under the strike
    assert variants.spot_exit_now(spec, "KXBTC15M", "no", 200.0, 100.05, now) is None  # the NO side is ahead
    assert variants.spot_exit_now(spec, "KXBTC15M", "yes", 400.0, 100.05, now) is None  # outside the last shock_last seconds


def test_a_spike_against_the_side_near_the_strike_is_sold_at_once_by_the_spike_exit(monkeypatch):
    now = time.time()
    vals = [100.0] * 150 + [100.0 - 0.03 * (k / 50) for k in range(1, 51)]  # 100.00 -> 99.97: 3 bp down in the last 50 s; the typical minute is 1 bp
    _ticks(monkeypatch, now, vals, unit=0.0001)
    spec = _spec()
    why = variants.spot_exit_now(spec, "KXBTC15M", "yes", 200.0, 99.965, now)  # 0.5 bp over the strike, inside 0.6 typical minutes
    assert why and "spike exit" in why
    assert variants.spot_exit_now(spec, "KXBTC15M", "yes", 200.0, 99.90, now) is None  # a spike but 10 bp of cushion: not near the strike
    assert variants.spot_exit_now(spec, "KXBTC15M", "no", 200.0, 99.965, now) is None  # the other side moved with the fall
    _ticks(monkeypatch, now, [100.0] * 200)
    assert variants.spot_exit_now(spec, "KXBTC15M", "yes", 200.0, 99.99, now) is None  # no move, a cushion: nothing to sell


def test_a_lane_without_spot_exits_or_an_unreadable_market_does_not_block(monkeypatch):
    now = time.time()
    _ticks(monkeypatch, now, [100.0] * 200)
    assert variants.spot_exit_now(_spec(shock_units=0.0, cross_units=-1.0), "KXBTC15M", "yes", 200.0, 100.05, now) is None
    assert variants.spot_exit_now(_spec(cross_units=0.0), "KXGOLD15M", "yes", 200.0, 100.05, now) is None  # not a crypto coin
    assert variants.spot_exit_now(_spec(cross_units=0.0), "KXBTC15M", "yes", 200.0, None, now) is None  # no strike
