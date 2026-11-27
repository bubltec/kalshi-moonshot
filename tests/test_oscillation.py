"""The oscillator reading of the spot's last half hour (oscillation.py; 2026-11-27, user: "We should be using oscillation mathematics and differential equations to calculate wave patterns ... reading the spot history back about 30 mins")."""
import math
import random

import pytest

from kalshi_moonshot import oscillation as osc

N = 181  # 30 minutes on the 10 s grid


def path(f, noise=0.15, seed=1):
    rnd = random.Random(seed)
    return [f(i) + rnd.gauss(0.0, noise) for i in range(N)]


def test_a_wave_is_read_with_its_period_its_count_and_no_damping():
    r = osc.read(path(lambda i: 100 + 2 * math.sin(2 * math.pi * i * 10 / 300)), 100.0)
    assert r["period_s"] == pytest.approx(300, rel=0.05) and r["counted_period_s"] == pytest.approx(300, rel=0.1)
    assert 5.0 <= r["waves"] <= 6.0 and abs(r["damping"]) < 0.05 and r["fit"] > 0.5
    assert r["trend_x"] < 1.0 and r["spikes"] == 0 and osc.kind(r) == "wave"
    assert abs(r["target_amp"]) < 0.3  # the waves swing across the target


def test_the_target_outside_the_swing_is_said_in_amplitudes():
    r = osc.read(path(lambda i: 100 + 2 * math.sin(2 * math.pi * i * 10 / 300)), 106.0)
    assert r["target_amp"] > 2.0  # three amplitudes over the centre: the waves do not reach it


def test_a_trend_is_a_trend_and_a_wave_on_a_trend_is_both():
    r = osc.read(path(lambda i: 100 + 0.05 * i, noise=0.3), 100.0)
    assert r["trend_x"] > 5.0 and osc.kind(r) == "trend"
    r = osc.read(path(lambda i: 100 + 0.03 * i + 1.5 * math.sin(2 * math.pi * i * 10 / 240), noise=0.1), 100.0)
    assert r["period_s"] == pytest.approx(240, rel=0.05) and osc.kind(r) == "trend+wave"


def test_a_flat_line_with_jumps_is_spikes():
    r = osc.read(path(lambda i: 100 + (3.0 if i > 120 else 0.0) + (2.0 if i > 60 else 0.0), noise=0.05), 100.0)
    assert r["spikes"] == 2 and r["spike_share"] > 0.8 and "spike" in osc.kind(r)


def test_the_phase_says_where_the_spot_is_in_its_swing():
    top = osc.read([100 + 2 * math.cos(2 * math.pi * (i - (N - 1)) * 10 / 300) for i in range(N)], 100.0)  # the last sample is a crest
    bottom = osc.read([100 - 2 * math.cos(2 * math.pi * (i - (N - 1)) * 10 / 300) for i in range(N)], 100.0)  # the last sample is a trough
    assert top["pos_now_amp"] > 0.7 and (top["phase"] < 0.1 or top["phase"] > 0.9)
    assert bottom["pos_now_amp"] < -0.7 and 0.4 < bottom["phase"] < 0.6


def test_too_little_path_or_a_path_that_does_not_move_is_unread():
    assert osc.read([100.0] * 30) is None and osc.read([100.0] * N) is None
