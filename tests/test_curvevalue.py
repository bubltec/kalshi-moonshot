import math

from kalshi_moonshot import curvevalue


def _series(now, secs=1100, base=100.0, wiggle=0.0, drift=0.0):
    ts = [now - secs + i for i in range(secs + 1)]
    vs = [base * math.exp(drift * i + wiggle * (1 if i % 2 else -1)) for i in range(secs + 1)]
    return ts, vs


def test_winning_cushion_in_a_flat_market_is_near_certain():
    now = 1_000_000.0
    ts, vs = _series(now, wiggle=1e-6)
    got = curvevalue.read(ts, vs, now, ref=99.9, side="yes", left=120.0)
    assert got is not None and got.cushion > 0 and got.p > 0.99


def test_side_flips_the_cushion_and_the_chance():
    now = 1_000_000.0
    ts, vs = _series(now, wiggle=1e-6)
    yes = curvevalue.read(ts, vs, now, ref=99.9, side="yes", left=120.0)
    no = curvevalue.read(ts, vs, now, ref=99.9, side="no", left=120.0)
    assert yes is not None and no is not None and no.cushion == -yes.cushion and no.p < 0.01


def test_a_spike_pulls_the_pessimistic_chance_down():
    now = 1_000_000.0
    ts, vs = _series(now, wiggle=1e-6)
    calm = curvevalue.read(ts, vs, now, ref=99.95, side="yes", left=60.0)
    for i in range(len(vs) - 120, len(vs)):  # the last two minutes swing hard
        vs[i] *= math.exp(8e-4 * (1 if i % 2 else -1))
    spiked = curvevalue.read(ts, vs, now, ref=99.95, side="yes", left=60.0)
    assert calm is not None and spiked is not None and spiked.rate_short > calm.rate_short and spiked.p < calm.p


def test_short_history_or_stale_tick_is_unusable():
    now = 1_000_000.0
    ts, vs = _series(now, secs=500)
    assert curvevalue.read(ts, vs, now, 99.9, "yes", 120.0) is None
    ts, vs = _series(now)
    assert curvevalue.read(ts, vs, now + 30.0, 99.9, "yes", 120.0) is None
    assert curvevalue.read(ts, vs, now, 0.0, "yes", 120.0) is None


def test_the_long_scale_alone_ignores_a_spike():
    now = 1_000_000.0
    ts, vs = _series(now, wiggle=1e-6)
    for i in range(len(vs) - 120, len(vs)):  # a hard swing in the last two minutes
        vs[i] *= math.exp(8e-4 * (1 if i % 2 else -1))
    both = curvevalue.read(ts, vs, now, ref=99.95, side="yes", left=60.0)
    long_only = curvevalue.read(ts, vs, now, ref=99.95, side="yes", left=60.0, scales="900")
    assert both is not None and long_only is not None and both.p <= long_only.p


def test_a_lane_with_only_the_curve_exit_is_watched():
    from kalshi_moonshot.lanes import LaneSpec, takes_profit

    def spec(**kw):
        return LaneSpec("x", "ladder", "x", (0.70, 0.97), (0, 1), 0.05, 0.0, **kw)

    assert not takes_profit(spec())
    assert takes_profit(spec(curve_exit=0.15))
