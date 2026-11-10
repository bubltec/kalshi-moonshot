import math
import random

from kalshi_moonshot import variants


def _series(now, hours=7.0, perp_jump=0.0, choppy_tail=True, seed=3):
    rnd = random.Random(seed)
    n = int(hours * 3600)
    ts, vs, pts, pvs = [], [], [], []
    x = 0.0
    for i in range(n + 1):
        tail = n - i
        if choppy_tail and tail < 300:
            x += 4e-4 * (1 if i % 2 else -1)  # a 300 s zigzag: a lot of path, no net move
        else:
            x += rnd.gauss(0, 1e-4)
        ts.append(now - tail)
        vs.append(100.0 * math.exp(x))
        if tail <= 3300:
            lift = perp_jump * min(1.0, max(0.0, (60 - tail) / 60.0)) if tail < 60 else 0.0
            pts.append(now - tail)
            pvs.append(100.0 * math.exp(x + lift))
    return ts, vs, pts, pvs


def test_the_perp_running_ahead_in_a_zigzag_near_the_strike_passes():
    now = 3_000_000.0
    ts, vs, pts, pvs = _series(now, perp_jump=0.003)
    got = variants.perp_rule_from(ts, vs, pts, pvs, now, "yes", strike=vs[-1])
    assert got is not None and got["ok"] == 1.0 and got["lead"] > got["lead_cut"]


def test_no_lead_does_not_pass_and_the_no_side_flips_the_lead():
    now = 3_000_000.0
    ts, vs, pts, pvs = _series(now, perp_jump=0.0)
    flat = variants.perp_rule_from(ts, vs, pts, pvs, now, "yes", strike=vs[-1])
    assert flat is not None and flat["ok"] == 0.0
    ts, vs, pts, pvs = _series(now, perp_jump=0.003)
    yes = variants.perp_rule_from(ts, vs, pts, pvs, now, "yes", strike=vs[-1])
    no = variants.perp_rule_from(ts, vs, pts, pvs, now, "no", strike=vs[-1])
    assert yes is not None and no is not None and no["lead"] < 0 < yes["lead"] and no["ok"] == 0.0


def test_a_straight_path_fails_the_choppy_test():
    now = 3_000_000.0
    ts, vs, pts, pvs = _series(now, perp_jump=0.003, choppy_tail=False)
    for i in range(len(vs) - 300, len(vs)):  # a straight 300 s run
        vs[i] = vs[len(vs) - 301] * math.exp(3e-4 * (i - (len(vs) - 301)))
    got = variants.perp_rule_from(ts, vs, pts, pvs, now, "yes", strike=vs[-1])
    assert got is not None and got["ok"] == 0.0 and got["eff"] > got["eff_cut"]


def test_short_or_stale_history_is_unreadable():
    now = 3_000_000.0
    ts, vs, pts, pvs = _series(now, hours=1.5)
    assert variants.perp_rule_from(ts, vs, pts, pvs, now, "yes", strike=vs[-1]) is None
    ts, vs, pts, pvs = _series(now)
    assert variants.perp_rule_from(ts, vs, pts, pvs, now + 120.0, "yes", strike=vs[-1]) is None
    assert variants.perp_rule_from(ts, vs, pts, pvs, now, "yes", strike=0.0) is None
