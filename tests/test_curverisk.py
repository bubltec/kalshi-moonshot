"""The single risk exit (curverisk.py, 2026-11-13, user): the worse of the price cushion and the spot cushion, widened by the hour's jumps, sold within a max loss or min profit, with a wait that scales with the time left."""

import math
import random
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


def test_a_sale_needs_high_risk_and_a_loss_or_a_real_profit():
    assert curverisk.sale_ok(0.40, 0.50, -0.20, 0.01)  # a small loss
    assert curverisk.sale_ok(0.40, 0.50, -0.70, 0.01)  # a deep one is cut too
    assert curverisk.sale_ok(0.40, 0.50, 0.10, 0.01)  # a profit
    assert not curverisk.sale_ok(0.60, 0.50, -0.20, 0.01)  # risk is low: hold
    assert not curverisk.sale_ok(0.40, 0.50, 0.005, 0.01)  # a profit under the minimum
    assert not curverisk.sale_ok(0.10, -1.0, -0.1, 0.01)  # off


def test_the_max_loss_sells_at_once_whatever_the_chance_reads():
    assert curverisk.stopped(-0.50, 0.5) and curverisk.stopped(-0.80, 0.5)
    assert not curverisk.stopped(-0.30, 0.5) and not curverisk.stopped(-0.99, 0.0)


def test_the_wait_scales_with_the_time_left_and_how_far_under_the_line():
    assert curverisk.wait(0.45, 0.5, 600.0, 40.0, 0.25) == 40.0 * (1 - 0.1)  # just under the line: nearly the whole recovery time
    assert curverisk.wait(0.25, 0.5, 600.0, 40.0, 0.25) == 20.0  # half way to zero: half
    assert curverisk.wait(0.0, 0.5, 600.0, 40.0, 0.25) == 0.0
    assert curverisk.wait(0.45, 0.5, 60.0, 40.0, 0.25) == 15.0  # a quarter of the time left caps it: 60 s left cannot wait 36 s
    assert curverisk.wait(0.45, 0.5, 20.0, 40.0, 0.25) == 5.0


CLOSE = 9_900.0


def target(**kw):
    base = dict(ticker="KXWTI15M-1", lane="x", side="no", entry=0.80, close_ts=CLOSE, shock_ref=100.0, shock_index="BQ_WTI", curve_scales="900+180", curve_wait_share=0.25, curve_wait_default=15.0,
                risk_sell=0.5, risk_live=False, risk_spot_weight=0.5, risk_history=False, risk_max_loss=0.5, risk_min_profit=0.01, risk_at=0.0, risk_noted=0.0, risk_since=0.0, risk_hist=1.0, risk_hist_at=0.0, risk_logged=False)
    base.update(kw)
    return SimpleNamespace(**base)


def run(monkeypatch, t, bid, p, seconds=30, left0=100.0):
    monkeypatch.setattr(watcher.ticks_mod.history, "window", lambda *a, **k: ([0.0], [1.0]))
    monkeypatch.setattr(curvevalue, "read", lambda *a, **k: curvevalue.Curve(p, 0.0, 1e-4, 1e-4))
    monkeypatch.setattr(watcher.variants, "comeback_stats", lambda *a, **k: None)
    logged = []
    monkeypatch.setattr(watcher.log, "info", lambda msg, *a, **k: logged.append(msg % a if a else msg))
    me = SimpleNamespace()
    out = None
    for i in range(seconds):
        now = CLOSE - left0 + i
        out = watcher.ProfitWatcher._risk_hit(me, t, bid, now, now)
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
    i, reason, _ = run(monkeypatch, target(risk_live=True), bid=0.30, p=0.85)  # a 63% loss: past the 50% max, the sale is at the first reading
    assert i == 0 and "(max loss)" in reason
