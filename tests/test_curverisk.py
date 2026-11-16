import pytest
"""The single risk exit (curverisk.py, 2026-11-13, user): the worse of the price cushion and the spot cushion, widened by the hour's jumps, sold within a max loss or min profit, with a wait that scales with the time left."""

import json
import math
import random
import tempfile
from pathlib import Path
from types import SimpleNamespace

from kalshi_moonshot import curverisk, curvevalue, watcher


def test_the_price_cushion_counts_in_full_and_the_spot_cushion_only_part_of_the_way():
    r = curverisk.risk(0.80, 0.55, 1.0, 0.5)  # the market pays less than the curve says: the price is the worse one
    assert r.worse == "price" and abs(r.q - 0.55) < 1e-9
    r = curverisk.risk(0.40, 0.80, 1.0, 0.5)  # the spot says danger, the market does not: half way down
    assert r.worse == "spot" and abs(r.q - 0.60) < 1e-9 and abs(r.q_spot - 0.40) < 1e-9
    assert abs(curverisk.risk(0.40, 0.80, 1.0, 1.0).q - 0.40) < 1e-9 and abs(curverisk.risk(0.40, 0.80, 1.0, 0.0).q - 0.80) < 1e-9


def test_a_jumpy_hour_pulls_the_spot_chance_toward_even():
    calm = curverisk.risk(0.90, 0.99, 1.0, 1.0).q_spot
    jumpy = curverisk.risk(0.90, 0.99, 2.0, 1.0).q_spot
    assert calm > jumpy > 0.5


def ticks(seconds, rng, spike_every=0):
    ts, vs, v = [], [], 100.0
    for i in range(seconds):
        step = rng.gauss(0.0, 1e-4)
        if spike_every and i % spike_every == 0:
            step += rng.choice((-1, 1)) * 1e-2
        v *= math.exp(step)
        ts.append(float(i))
        vs.append(v)
    return ts, vs


def test_history_factor_is_one_for_an_even_hour_and_larger_when_the_hour_is_bunched_into_jumps():
    rng = random.Random(1)
    ts, vs = ticks(3700, rng)
    even = curverisk.history_factor(ts, vs, 3699.0)
    ts, vs = ticks(3700, rng, spike_every=300)
    spiky = curverisk.history_factor(ts, vs, 3699.0)
    assert 1.0 <= even < 1.4 and spiky > 2.0
    assert curverisk.history_factor([0.0, 1.0], [100.0, 100.0], 1.0) == 1.0  # too short to read
    assert curverisk.history_factor(list(map(float, range(3700))), [100.0] * 3700, 3699.0) == 1.0  # flat: nothing to read


def test_a_sale_needs_high_risk_and_a_loss_or_a_profit_that_shrinks_with_the_risk():
    assert curverisk.sale_ok(0.40, 0.50, -0.20, 0.05)  # a small loss
    assert curverisk.sale_ok(0.40, 0.50, -0.70, 0.05)  # a deep one is cut too
    assert curverisk.sale_ok(0.40, 0.50, 0.10, 0.05)  # a profit
    assert not curverisk.sale_ok(0.60, 0.50, -0.20, 0.05)  # risk is low: hold
    assert not curverisk.sale_ok(0.45, 0.50, 0.01, 0.10)  # asked 0.10 * 0.45 = 0.045, got 0.01
    assert curverisk.sale_ok(0.10, 0.50, 0.02, 0.10)  # at q 0.10 the ask is 0.01: taken
    assert not curverisk.sale_ok(0.10, -1.0, -0.1, 0.05)  # off


def test_the_loss_we_carry_shrinks_with_the_risk():
    assert not curverisk.stopped(-0.30, 0.9, 0.5)  # q 0.9: limit 45%
    assert curverisk.stopped(-0.46, 0.9, 0.5)
    assert curverisk.stopped(-0.30, 0.5, 0.5)  # q 0.5: limit 25%
    assert not curverisk.stopped(-0.20, 0.5, 0.5) and not curverisk.stopped(-0.99, 0.5, 0.0)


def test_the_wait_scales_with_the_time_left_and_how_far_under_the_line():
    assert curverisk.wait(0.45, 0.5, 600.0, 40.0, 0.25) == 40.0 * (1 - 0.1)  # just under the line: nearly the whole recovery time
    assert curverisk.wait(0.25, 0.5, 600.0, 40.0, 0.25) == 20.0  # half way to zero: half
    assert curverisk.wait(0.0, 0.5, 600.0, 40.0, 0.25) == 0.0
    assert curverisk.wait(0.45, 0.5, 60.0, 40.0, 0.25) == 15.0  # a quarter of the time left caps it: 60 s left cannot wait 36 s
    assert curverisk.wait(0.45, 0.5, 20.0, 40.0, 0.25) == 5.0


CLOSE = 9_900.0


def target(**kw):
    base = dict(ticker="KXWTI15M-1", lane="x", side="no", entry=0.80, close_ts=CLOSE, shock_ref=100.0, shock_index="BQ_WTI", curve_scales="900+180", curve_wait_share=0.25, curve_wait_default=15.0,
                risk_sell=0.5, risk_live=False, risk_spot_weight=0.5, risk_history=False, risk_max_loss=0.5, risk_early_hold=0.0, risk_early_off=False, risk_min_profit=0.01, risk_stop_hold=0.0, risk_take=False, risk_take_slope=10.0, risk_take_hist=[], risk_squeeze=0.0, risk_unit=None, risk_unit_at=0.0, risk_at=0.0, risk_noted=0.0, risk_since=0.0, risk_hist=1.0, risk_hist_at=0.0, risk_logged=False, risk_confirm=-1.0, risk_long=None, risk_long_at=0.0, retry_after=0.0, risk_long_fallback=False, risk_long_missing=False)
    base.setdefault("risk_held_kind", "")
    base.setdefault("risk_nudge_units", 0.0)
    base.setdefault("risk_nudge_hour", False)
    base.update(kw)
    return SimpleNamespace(**base)


def run(monkeypatch, t, bid, p, seconds=30, left0=100.0, tmp=None):
    monkeypatch.setattr(watcher.ticks_mod.history, "window", lambda *a, **k: ([0.0], [1.0]))
    monkeypatch.setattr(curvevalue, "read", lambda *a, **k: curvevalue.Curve(p, 0.0, 1e-4, 1e-4))
    monkeypatch.setattr(watcher.variants, "comeback_stats", lambda *a, **k: None)
    logged = []
    monkeypatch.setattr(watcher.log, "info", lambda msg, *a, **k: logged.append(msg % a if a else msg))
    me = SimpleNamespace()
    me._risk_decide = lambda *a: watcher.ProfitWatcher._risk_decide(me, *a)
    me._risk_record = lambda *a, **k: watcher.ProfitWatcher._risk_record(me, *a, **k)
    me._risk_held = lambda *a: watcher.ProfitWatcher._risk_held(me, *a)
    me._risk_long = lambda *a: watcher.ProfitWatcher._risk_long(me, *a)
    tmp = Path(tmp) if tmp else Path(tempfile.mkdtemp())
    me.get_settings = lambda: SimpleNamespace(data_dir=tmp)
    out = None
    for i in range(seconds):
        now = CLOSE - left0 + i
        out = watcher.ProfitWatcher._risk_hit(me, t, bid, now, now) if t.risk_spot_weight > 0 else watcher.ProfitWatcher._risk_quote_hit(me, t, bid, now)
        if out:
            return i, out, logged
    return None, None, logged


def test_live_it_sells_when_the_price_cushion_is_under_the_line_and_the_loss_is_small(monkeypatch):
    t = target(risk_live=True)
    _, reason, _ = run(monkeypatch, t, bid=0.48, p=0.85)  # the market says 0.52, the curve says 0.85: price is the worse one
    assert reason and reason.startswith("curve risk exit") and "price cushion the worse" in reason


def test_shadow_logs_the_sale_once_and_sells_nothing(monkeypatch):
    t = target(risk_live=False)
    _, reason, logged = run(monkeypatch, t, bid=0.48, p=0.85, seconds=60)
    assert reason is None and sum("curve risk shadow" in m for m in logged) == 1


def test_a_high_chance_holds_and_the_max_loss_sells_at_once(monkeypatch):
    assert run(monkeypatch, target(risk_live=True), bid=0.80, p=0.90)[1] is None  # plenty of cushion
    i, reason, _ = run(monkeypatch, target(risk_live=True), bid=0.30, p=0.85)  # a 63% loss at q 0.28: past the limit, the sale is at the first reading
    assert i == 0 and "(max loss)" in reason


def test_the_hours_history_is_read_from_the_kalshi_perp_not_the_spot_feed(monkeypatch):
    assert watcher.perp_index("KXWTI15M") == "KP_WTI" and watcher.perp_index("KXBTC15M") == "KP_BTC" and watcher.perp_index("KXBTCD") is None and watcher.perp_index("KXNOPE15M") is None
    rng = random.Random(3)
    sts, svs = ticks(3700, rng, spike_every=300)
    spiky = ([x + 6100.0 for x in sts], svs)  # the last tick lands just before now = 9,800
    asked = []

    def window(index, a, b):
        asked.append(index)
        return spiky if index == "KP_WTI" else ([0.0], [1.0])

    monkeypatch.setattr(watcher.ticks_mod.history, "window", window)
    monkeypatch.setattr(curvevalue, "read", lambda *a, **k: curvevalue.Curve(0.85, 0.0, 1e-4, 1e-4))
    monkeypatch.setattr(watcher.variants, "comeback_stats", lambda *a, **k: None)
    monkeypatch.setattr(watcher.log, "info", lambda *a, **k: None)
    t = target(risk_history=True, risk_live=True)
    me = SimpleNamespace()
    me._risk_decide = lambda *a: watcher.ProfitWatcher._risk_decide(me, *a)
    me._risk_record = lambda *a, **k: watcher.ProfitWatcher._risk_record(me, *a, **k)
    me._risk_held = lambda *a: watcher.ProfitWatcher._risk_held(me, *a)
    me._risk_long = lambda *a: watcher.ProfitWatcher._risk_long(me, *a)
    me.get_settings = lambda: SimpleNamespace(data_dir=Path(tempfile.mkdtemp()))
    watcher.ProfitWatcher._risk_hit(me, t, 0.80, CLOSE - 100.0, CLOSE - 100.0)
    assert "KP_WTI" in asked and t.risk_hist > 1.5


def test_the_price_only_exit_needs_no_spot_feed_and_writes_its_shadow_record(monkeypatch, tmp_path):
    t = target(risk_spot_weight=0.0, shock_ref=0.0, shock_index="", risk_live=False)  # no spot reference at all
    _, reason, logged = run(monkeypatch, t, bid=0.48, p=0.85, seconds=60, tmp=tmp_path)
    assert reason is None and sum("curve risk shadow" in m for m in logged) == 1
    rows = (tmp_path / "risk_shadow.jsonl").read_text().splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["worse"] == "price" and json.loads(rows[0])["ticker"] == "KXWTI15M-1"


def test_live_the_price_only_exit_sells_at_the_max_loss_with_no_wait(monkeypatch, tmp_path):
    t = target(risk_spot_weight=0.0, shock_ref=0.0, shock_index="", risk_live=True)
    i, reason, _ = run(monkeypatch, t, bid=0.30, p=0.85, tmp=tmp_path)
    assert i == 0 and "(max loss)" in reason
    rows = [json.loads(x) for x in (tmp_path / "risk_shadow.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["live"] is True and rows[0]["stop"] is True


def test_the_spot_path_leaves_a_price_only_lane_alone_and_the_quote_path_a_spot_lane(monkeypatch):
    me = SimpleNamespace()
    assert watcher.ProfitWatcher._risk_hit(me, target(risk_spot_weight=0.0, risk_live=True), 0.30, CLOSE - 100.0, CLOSE - 100.0) is None
    assert watcher.ProfitWatcher._risk_quote_hit(me, target(risk_spot_weight=0.5, risk_live=True), 0.30, CLOSE - 100.0) is None


def test_a_lane_with_only_the_risk_exit_still_gets_a_watched_position():
    from kalshi_moonshot import lanes

    def spec(**kw):
        return lanes.LaneSpec("x", "ladder", "x", (0.70, 0.97), (0, 1), 0.05, 0.0, **kw)

    assert lanes.takes_profit(spec(curve_risk_sell=0.5)) and not lanes.takes_profit(spec())


def test_long_chance_reads_the_cushion_against_the_hours_own_movement():
    rng = random.Random(5)
    ts, vs = ticks(3700, rng)
    now = 3699.0
    ref = vs[-1]
    flat = curverisk.long_chance(ts, vs, now, ref, "yes", 120.0)
    assert flat is not None and abs(flat - 0.5) < 0.01  # sitting on the reference: a coin flip
    above = curverisk.long_chance(ts, vs, now, ref * 0.997, "yes", 120.0)
    assert above is not None and above > 0.9  # 30 bp over the open in a quiet hour with 2 minutes left
    assert curverisk.long_chance(ts, vs, now, ref * 0.997, "no", 120.0) < 0.1
    assert curverisk.long_chance(ts[:100], vs[:100], 99.0, ref, "yes", 120.0) is None  # too short an hour
    assert curverisk.long_chance(ts, vs, now + 100.0, ref, "yes", 120.0) is None  # the newest tick is stale
    assert curverisk.long_chance(ts, vs, now, 0.0, "yes", 120.0) is None


def test_a_price_dip_alone_does_not_sell_when_the_long_layer_says_the_bet_stays_positive(monkeypatch, tmp_path):
    t = target(risk_spot_weight=0.0, shock_ref=0.0, shock_index="", risk_live=True, risk_confirm=0.4)
    logged = []
    monkeypatch.setattr(watcher.log, "info", lambda msg, *a, **k: logged.append(msg % a if a else msg))
    me = SimpleNamespace(get_settings=lambda: SimpleNamespace(data_dir=tmp_path))
    me._risk_record = lambda *a, **k: watcher.ProfitWatcher._risk_record(me, *a, **k)
    me._risk_held = lambda *a: watcher.ProfitWatcher._risk_held(me, *a)
    monkeypatch.setattr(watcher.variants, "comeback_stats", lambda *a, **k: None)
    me._risk_long = lambda tt, now, left: 0.9  # the hour says the side is very likely to pay
    me._risk_decide = lambda *a: watcher.ProfitWatcher._risk_decide(me, *a)
    assert watcher.ProfitWatcher._risk_quote_hit(me, t, 0.30, CLOSE - 100.0) is None  # a 63% loss, past the max-loss limit, price alone would sell
    me._risk_long = lambda tt, now, left: 0.2  # the hour agrees it is going wrong
    t.risk_at = 0.0
    assert watcher.ProfitWatcher._risk_quote_hit(me, t, 0.30, CLOSE - 99.0).startswith("curve risk exit (max loss)")
    me._risk_long = lambda tt, now, left: None  # no hour to read: the sale does not wait for it
    t.risk_at = 0.0
    assert watcher.ProfitWatcher._risk_quote_hit(me, t, 0.30, CLOSE - 98.0) is not None


def test_under_the_minimum_bid_the_line_sale_does_not_fire_either(monkeypatch, tmp_path):
    """A bid of a few cents has nothing left to sell for: the line sale stood down there as the max loss already did (the BTC bet of 2026-11-13 was offered at 1 to 4 cents)."""
    t = target(risk_spot_weight=0.0, shock_ref=0.0, shock_index="", risk_live=True, risk_max_loss=0.0)  # no max loss: only the line could sell
    assert run(monkeypatch, t, bid=0.03, p=0.85, seconds=40, tmp=tmp_path)[1] is None
    t2 = target(risk_spot_weight=0.0, shock_ref=0.0, shock_index="", risk_live=True, risk_max_loss=0.0)
    assert run(monkeypatch, t2, bid=0.30, p=0.85, seconds=40, tmp=tmp_path)[1] is not None


def test_unit_take_profit_bar_is_the_share_of_the_bets_room_the_market_has_used():
    # the same gain of 0.05 on a bet with 0.20 of room: early it is plenty, late it is not
    assert curverisk.take_profit(0.05, 0.20, 800, 900, 0.01) is True  # 11% of the market gone, bar 0.022
    assert curverisk.take_profit(0.05, 0.20, 450, 900, 0.01) is False  # half gone, bar 0.10
    assert curverisk.take_profit(0.05, 0.20, 100, 900, 0.01) is False
    # a bigger room is a bigger bar at the same time: the bet itself sets the scale
    assert curverisk.take_profit(0.05, 0.60, 850, 900, 0.01) is True
    assert curverisk.take_profit(0.05, 0.60, 600, 900, 0.01) is False  # a third gone, bar 0.20 vs 0.07
    # the near-certain gain at the very end rides to settlement
    assert curverisk.take_profit(0.19, 0.20, 10, 900, 0.01) is False


def test_unit_take_profit_needs_a_gain_over_the_buy_back_fee_and_a_live_clock():
    assert curverisk.take_profit(0.01, 0.20, 800, 900, 0.02) is False
    assert curverisk.take_profit(-0.01, 0.20, 800, 900, 0.0) is False
    assert curverisk.take_profit(0.05, 0.0, 800, 900, 0.01) is False
    assert curverisk.take_profit(0.05, 0.20, 0, 900, 0.01) is False


def test_unit_still_rising_follows_the_short_slope():
    up = [(float(t), 0.80 + 0.01 * t) for t in range(0, 11)]  # a climb to 0.90
    assert curverisk.still_rising(up, 10.0, 10.0) is True
    turned = up + [(11.0, 0.89)]
    assert curverisk.still_rising(turned, 11.0, 10.0) is False  # under the window's high
    flat = [(float(t), 0.90) for t in range(0, 12)]
    assert curverisk.still_rising(flat, 11.0, 10.0) is False  # levelled out: back at the start
    assert curverisk.still_rising([(5.0, 0.8), (6.0, 0.9)], 6.0, 10.0) is True  # under 10 s of readings: unknown, no sale
    assert curverisk.still_rising(up, 10.0, 0.0) is False  # no look back asked


def test_unit_late_in_the_market_a_flat_bid_holds_and_only_a_dip_sells():
    # a typical minute of 0.10: the noise over 10 s is 0.041
    flat = [(float(t), 0.90) for t in range(0, 11)]
    assert curverisk.still_rising(flat, 10.0, 10.0, 0.10) is False  # the live test outside the last fifth: flat sells
    assert curverisk.still_rising(flat, 10.0, 10.0, 0.10, flat_ok=True) is True  # in the last fifth a flat bid is held
    up_then_flat = [(float(t), 0.80 + 0.01 * min(t, 5)) for t in range(0, 11)]
    assert curverisk.still_rising(up_then_flat, 10.0, 10.0, 0.10, flat_ok=True) is True
    pause = up_then_flat + [(11.0, 0.84)]  # one tick back, inside the noise
    assert curverisk.still_rising(pause, 11.0, 10.0, 0.10, flat_ok=True) is True
    dip = up_then_flat + [(11.0, 0.80)]  # 0.05 off the high: more than the noise: it has started to dip
    assert curverisk.still_rising(dip, 11.0, 10.0, 0.10, flat_ok=True) is False
    assert curverisk.still_rising(flat, 10.0, 10.0, None, flat_ok=True) is True  # no unit: the newest bid is at the high
    assert curverisk.still_rising(flat + [(11.0, 0.89)], 11.0, 10.0, None, flat_ok=True) is False


def test_unit_a_flat_bid_is_held_late_only_while_the_cushion_backs_the_price():
    # a typical minute of 0.10 and 120 s left: a swing of 0.141
    assert curverisk.flat_holds(0.94, 0.90, 0.10, 120.0) is True  # the chance is 0.04 under what the sale pays: inside the swing
    assert curverisk.flat_holds(0.94, 0.99, 0.10, 120.0) is True  # the chance is over the price
    assert curverisk.flat_holds(0.95, 0.61, 0.10, 30.0) is False  # SOL NO 15:30: a chance of 0.61 against 0.95, a gap of 0.34 against a swing of 0.07
    assert curverisk.flat_holds(0.94, None, 0.10, 120.0) is False  # no reading of the cushion
    assert curverisk.flat_holds(0.94, 0.90, None, 120.0) is False  # no reading of the unit


def test_unit_a_climb_that_inflated_its_own_noise_is_still_a_slope():
    # SOL 21:15 YES, 2026-11-14: over the 43 s look back the bid rose 0.102 (0.880 to 0.982) in a market whose 15-minute typical minute, run-up included, was 0.124: a noise bar of 0.105
    path = [(float(t), 0.880 + 0.102 * t / 43.0) for t in range(0, 44)]
    assert curverisk.still_rising(path, 43.0, 43.0, 0.124) is True
    pause = path + [(44.0, 0.981)]  # the last tick back by 0.001, as it was
    assert curverisk.still_rising(pause, 44.0, 43.0, 0.124) is True


def test_unit_still_rising_in_the_markets_own_noise():
    # a typical minute of 0.10: the noise over 10 s is 0.10 * sqrt(10 / 60) = 0.041
    up = [(float(t), 0.80 + 0.01 * t) for t in range(0, 11)]  # +0.10 in 10 s
    assert curverisk.still_rising(up, 10.0, 10.0, 0.10) is True
    pause = up + [(11.0, 0.89)]  # one tick back: inside the noise, the rise over the look back is still 0.08
    assert curverisk.still_rising(pause, 11.0, 10.0, 0.10) is True
    assert curverisk.still_rising(pause, 11.0, 10.0, None) is False  # the plain test calls the same tick a turn
    fell = up + [(11.0, 0.85)]  # 0.05 off the high: more than the noise
    assert curverisk.still_rising(fell, 11.0, 10.0, 0.10) is False
    slow = [(float(t), 0.80 + 0.002 * t) for t in range(0, 11)]  # +0.02 in 10 s: under this market's noise
    assert curverisk.still_rising(slow, 10.0, 10.0, 0.10) is True  # any rise at the high is a slope (2026-11-14: the rise no longer has to beat the noise)
    assert curverisk.still_rising(slow, 10.0, 10.0, 0.01) is True  # and in a quiet market too
    flat_top = [(float(t), 0.80) for t in range(0, 11)]
    assert curverisk.still_rising(flat_top, 10.0, 10.0, 0.10) is False  # no rise at all is not a slope
    level = up + [(float(t), 0.90) for t in range(11, 22)]  # the climb has aged out of the look back
    assert curverisk.still_rising(level, 21.0, 10.0, 0.10) is False


# ---- the squeeze on sales (2026-11-14, user: "If something missed the bar to sell and history thinks we should ... minimizing loss is always more important") ----
def test_unit_still_falling_is_a_slide_that_has_lasted_not_one_bad_print():
    slide = [(float(t), 0.80 - 0.01 * t) for t in range(0, 11)]  # -0.10 in 10 s
    assert curverisk.still_falling(slide, 10.0, 10.0, 0.10) is True  # noise over 10 s is 0.041
    crash = [(float(t), 0.80) for t in range(0, 10)] + [(10.0, 0.60)]
    assert curverisk.still_falling(crash, 10.0, 10.0, 0.10) is False  # one print: half the look back ago it was still at the start
    stayed = crash + [(float(t), 0.60) for t in range(11, 17)]
    assert curverisk.still_falling(stayed, 16.0, 10.0, 0.10) is True  # it has stayed down for over half the look back
    bounced = slide + [(11.0, 0.76)]
    assert curverisk.still_falling(bounced, 11.0, 10.0, 0.10) is False  # 0.06 off the low: more than the noise
    steep = [(float(t), 0.80 - 0.02 * t) for t in range(0, 11)] + [(11.0, 0.66)]  # down 0.20, then 0.06 back up
    assert curverisk.still_falling(steep, 11.0, 10.0, 0.10) is False  # still far under its start, but it has bounced by more than the noise
    drift = [(float(t), 0.80 - 0.002 * t) for t in range(0, 11)]
    assert curverisk.still_falling(drift, 10.0, 10.0, 0.10) is False  # -0.02 in 10 s: inside this market's noise
    assert curverisk.still_falling(drift, 10.0, 10.0, 0.01) is True  # the same slide in a quiet market
    assert curverisk.still_falling(slide[-3:], 10.0, 10.0, 0.10) is False  # under 10 s of readings: unknown, no squeeze
    assert curverisk.still_falling(slide, 10.0, 0.0, 0.10) is False


def test_unit_stop_miss_is_zero_at_the_stop_and_positive_short_of_it():
    # cost 0.80, max loss 0.5 x q: the stop is where net - 0.80 = -0.5 * net * 0.80, a net bid of 0.571
    assert curverisk.stop_miss(0.5714, 0.80, 0.5714, 0.5) == pytest.approx(0.0, abs=1e-3)
    assert curverisk.stop_miss(0.60, 0.80, 0.60, 0.5) == pytest.approx(0.04)
    assert curverisk.stop_miss(0.50, 0.80, 0.50, 0.5) < 0
    assert (curverisk.stop_miss(0.56, 0.80, 0.56, 0.5) <= 0) is curverisk.stopped((0.56 - 0.80) / 0.80, 0.56, 0.5)


# ---- the late cap on the profit take's bar (2026-11-14, user, on a SOL NO back at 0.96 with 30 s left that lost at the bell: "Yes we should have cashed that out") ----
def test_unit_late_in_a_swinging_market_the_bar_is_half_the_room_once_more_is_made_than_is_left():
    # the SOL bet: room 0.20, 0.157 made, 30 s left, a typical minute of 0.15 (a swing of 0.106 over 30 s against 0.043 left to win)
    assert curverisk.take_bar(0.157, 0.20, 30, 900, 0.15) == pytest.approx(0.10)
    assert curverisk.take_profit(0.157, 0.20, 30, 900, 0.01, 0.15) is True
    assert curverisk.take_profit(0.157, 0.20, 30, 900, 0.01) is False  # the clock's bar alone (0.193) held it: what happened live


def test_unit_in_a_market_too_quiet_to_take_it_back_the_clocks_bar_stands():
    assert curverisk.take_bar(0.157, 0.20, 30, 900, 0.01) == pytest.approx(0.20 * (1 - 30 / 900))  # a swing of 0.007 against 0.043 left to win
    assert curverisk.take_profit(0.157, 0.20, 30, 900, 0.01, 0.01) is False


def test_unit_the_cap_never_lets_through_a_gain_under_half_the_room():
    assert curverisk.take_profit(0.09, 0.20, 60, 900, 0.01, 0.15) is False  # 0.11 left to win is inside the swing, but 0.09 is under half of 0.20
    assert curverisk.take_profit(0.11, 0.20, 60, 900, 0.01, 0.15) is True


def test_unit_early_the_clocks_bar_is_already_lower_and_nothing_changes():
    for unit in (None, 0.01, 0.15):
        assert curverisk.take_bar(0.05, 0.20, 800, 900, unit) == pytest.approx(0.20 * (1 - 800 / 900))
    assert curverisk.take_bar(0.05, 0.0, 800, 900, 0.15) == float("inf")


# ---- the stop's wait: capped by the market's recovery time, shorter with depth, scaled by the time left (2026-11-14) ----
def test_unit_stop_wait_is_the_recovery_time_cut_by_depth_and_by_the_clock():
    assert curverisk.stop_wait(0.60, 0.5, 900, 40.0, 0.5) == pytest.approx(40.0)  # over the line at the open: the whole recovery time
    assert curverisk.stop_wait(0.60, 0.5, 450, 40.0, 0.5) == pytest.approx(20.0)  # half the market gone: half the patience
    assert curverisk.stop_wait(0.25, 0.5, 450, 40.0, 0.5) == pytest.approx(10.0)  # and half way from the line to nothing: half again
    assert curverisk.stop_wait(0.0, 0.5, 450, 40.0, 0.5) == 0.0  # priced at nothing: no wait
    assert curverisk.stop_wait(0.44, 0.5, 215, 40.0, 0.5) == pytest.approx(40.0 * 0.88 * 215 / 900)  # the ZEC bet at 18:41:25: about 8 s; it was 107 s


def test_unit_stop_wait_never_exceeds_the_lanes_share_of_the_time_left_and_zero_is_at_once():
    assert curverisk.stop_wait(0.60, 0.5, 450, 400.0, 0.5) == pytest.approx(200.0)  # a market whose falls take long to come back: capped at... its own clock-scaled time, here 200 s
    assert curverisk.stop_wait(0.60, 0.5, 450, 4000.0, 0.5) == pytest.approx(225.0)  # and never over half the time left
    assert curverisk.stop_wait(0.60, 0.5, 450, 40.0, 0.0) == 0.0  # the lane has no wait: the stop sells at once
    assert curverisk.stop_wait(0.60, 0.5, 0, 40.0, 0.5) == 0.0
    assert curverisk.stop_wait(0.60, 0.0, 450, 40.0, 0.5) == pytest.approx(20.0)  # a lane with no line: no depth to read, the clock still applies


def test_unit_the_squeeze_clock_is_nothing_in_the_first_half_of_a_market():
    # 2026-11-14 (user: "squeeze shouldn't even apply at 6 minutes in"): none until the market is half gone, then rising to all of it at the close
    assert curverisk.squeeze_clock(900.0) == 0.0
    assert curverisk.squeeze_clock(540.0) == 0.0  # six minutes in: it was 0.4 of the cap before
    assert curverisk.squeeze_clock(450.0) == 0.0  # the half way point
    assert abs(curverisk.squeeze_clock(300.0) - 1.0 / 3.0) < 1e-9
    assert abs(curverisk.squeeze_clock(120.0) - (2.0 * (1.0 - 120.0 / 900.0) - 1.0)) < 1e-9
    assert curverisk.squeeze_clock(0.0) == 1.0
    assert curverisk.squeeze_clock(-5.0) == 1.0 and curverisk.squeeze_clock(500.0, 0.0) == 0.0
    from kalshi_moonshot.lanes import nudge

    assert nudge(0.02, 1.0, curverisk.squeeze_clock(540.0)) == 0.0  # six minutes in the nudge does not exist
    assert nudge(0.02, 1.0, curverisk.squeeze_clock(120.0)) > 0.014  # with two minutes left it is about three quarters of the cap


def test_unit_the_hour_views_hold_fades_over_the_last_fifth_of_the_market():
    # 2026-11-15: the bar the long layer must reach to hold a risk sale is the lane's 0.4 until the last fifth (180 s of 900), then rises to 1.0 at the close
    assert curverisk.confirm_bar(0.4, 900.0) == 0.4
    assert curverisk.confirm_bar(0.4, 181.0) == 0.4
    assert curverisk.confirm_bar(0.4, 180.0) == 0.4
    assert abs(curverisk.confirm_bar(0.4, 90.0) - 0.7) < 1e-9
    assert abs(curverisk.confirm_bar(0.4, 37.0) - (0.4 + 0.6 * (1.0 - 37.0 / 180.0))) < 1e-9
    assert curverisk.confirm_bar(0.4, 0.0) == 1.0 and curverisk.confirm_bar(0.4, -5.0) == 1.0
    assert curverisk.confirm_bar(-1.0, 50.0) == -1.0  # a lane that does not ask for the long layer is left as it was
