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


def _shot(side="yes"):
    from kalshi_moonshot.strategy import LongShot

    return LongShot(ticker="KXBTC15M-X", title="", rules="", bid=0.38, ask=0.40, volume=0.0, close_time="2099-01-01T00:00:00Z", hours_to_close=0.05, series="KXBTC15M", side=side, lane="m")


def test_the_buy_again_block_holds_the_side_just_sold_for_a_gain_and_the_spike_exit_state(monkeypatch):
    import time

    from kalshi_moonshot import runner

    now = time.time()
    store = SimpleNamespace(last_gain_sale=lambda t, lane: ("yes", now - 30.0), window_strike=lambda t: 100.0)
    spec = SimpleNamespace(rebuy_opposite_after_gain_seconds=120.0, entry_checks_exit=False)
    why = runner._buy_again_block(store, spec, _shot("yes"))
    assert why and "not buying back yet" in why  # sold YES for a gain 30 s ago: wait the rest of the 120 s
    assert runner._buy_again_block(store, spec, _shot("no")) is None  # the other side goes through the usual gate at once
    store.last_gain_sale = lambda t, lane: ("yes", now - 300.0)
    assert runner._buy_again_block(store, spec, _shot("yes")) is None  # the wait is over
    monkeypatch.setattr(variants, "spot_exit_now", lambda *a, **k: "the spike exit would sell it at once")
    spec2 = SimpleNamespace(rebuy_opposite_after_gain_seconds=0.0, entry_checks_exit=True)
    store.last_gain_sale = lambda t, lane: None
    assert "spike exit" in runner._buy_again_block(store, spec2, _shot("yes"))


def test_a_resting_bid_taken_at_the_fresh_ask_is_held_to_the_same_two_rules(monkeypatch):
    from kalshi_moonshot import runner

    monkeypatch.setattr(runner, "best_ask", lambda book, side: 0.40)
    monkeypatch.setattr(runner, "_buy_again_block", lambda store, spec, shot: "not buying: the spike exit would sell it at once")
    spec = SimpleNamespace(price=(0.20, 0.85), buy_slip=0.0, buy_retry_chase=0.05, model_buy=False, record_buy=False)
    client = SimpleNamespace(orderbook=lambda ticker: {})
    assert runner._requote(None, client, SimpleNamespace(), _shot("yes"), spec, 3.0) is None  # blocked: no take
    monkeypatch.setattr(runner, "_buy_again_block", lambda store, spec, shot: None)
    monkeypatch.setattr(runner.risk, "check", lambda *a, **k: SimpleNamespace(ok=True))
    monkeypatch.setattr(runner, "_cap_equity", lambda *a, **k: 100.0)
    assert runner._requote(None, client, SimpleNamespace(), _shot("yes"), spec, 3.0) == 0.40  # not blocked: the take goes on to the risk gate
