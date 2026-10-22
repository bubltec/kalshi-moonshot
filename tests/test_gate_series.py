import pytest

from kalshi_moonshot import gate
from kalshi_moonshot.gate import Cell


def cell(n, mean, se, band=(12, 15)):
    return Cell(band, n, mean, se, 0)


def test_a_market_with_no_bets_takes_the_pools_verdict():
    pooled = cell(200, 0.10, 0.03)  # sd = 0.03 * sqrt(200)
    ok, text = gate.decide_series(None, pooled, "KXWTI15M", 30, 1.0, 30)
    assert ok and "0 own bets" in text
    ok, _ = gate.decide_series(None, cell(200, -0.10, 0.03), "KXWTI15M", 30, 1.0, 30)
    assert not ok


def test_a_markets_own_bad_record_overrides_a_good_pool():
    pooled = cell(400, 0.12, 0.03)
    bad = cell(60, -0.40, 0.10)
    ok, _ = gate.decide_series(bad, pooled, "KXSILVER15M", 30, 1.0, 30)
    assert not ok
    mean, _, n = gate.shrink(bad, pooled, 30)
    assert n == 60 and -0.40 < mean < 0.12  # pulled toward the pool, not all the way


def test_a_thin_good_record_is_not_trusted_on_its_own():
    pooled = cell(400, -0.05, 0.03)  # the pool is slightly negative
    lucky = cell(5, 0.80, 0.30)
    ok, _ = gate.decide_series(lucky, pooled, "KXGOLD15M", 30, 1.0, 30)
    assert not ok  # five lucky bets do not open a band the pool says is closed


def test_the_pool_needs_enough_bets_before_any_market_may_open():
    ok, text = gate.decide_series(cell(40, 0.5, 0.05), cell(10, 0.5, 0.05), "KXWTI15M", 30, 1.0, 30)
    assert not ok and "needs 30" in text


def test_shrink_weights_own_and_pool_by_bet_counts():
    mean, se, n = gate.shrink(cell(30, 0.20, 0.05), cell(300, 0.0, 0.02), 30)
    assert mean == pytest.approx(0.10) and n == 30 and se > 0
