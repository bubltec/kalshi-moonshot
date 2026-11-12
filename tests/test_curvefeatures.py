import itertools
import math

from kalshi_moonshot import curvefeatures as cf


def _grid(n, f):
    g = [f(i) for i in range(n)]
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + (b - a) ** 2)
    return g, sq


def test_a_steady_climb_is_efficient_and_never_crosses():
    n = 3 * 3600
    g, sq = _grid(n, lambda i: 0.0001 * i + 0.01 * math.sin(i / 7.0) * 0.01)
    f = cf.moment_features(g, sq, n - 1, strike_log=g[0] - 0.001, sign=1)
    assert f["eff_13m"] > 0.9 and f["cross_13m"] == 0 and f["level_13m"] > 0 and f["slope_13m"] > 0
    assert f["fromhigh_5m"] < 0.1


def test_the_other_side_flips_level_and_slope_not_range():
    n = 3 * 3600
    g, sq = _grid(n, lambda i: 0.0001 * i + 0.00002 * math.sin(i / 50.0))
    a = cf.moment_features(g, sq, n - 1, g[0], 1)
    b = cf.moment_features(g, sq, n - 1, g[0], -1)
    assert math.isclose(a["level_13m"], -b["level_13m"]) and math.isclose(a["range_13m"], b["range_13m"])


def test_a_flat_curve_leaves_features_out_instead_of_zero():
    n = 3 * 3600
    g, sq = _grid(n, lambda i: 1.0)
    assert cf.moment_features(g, sq, n - 1, 1.0, 1) == {}
