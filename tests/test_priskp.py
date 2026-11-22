"""PriskP's readings, own-history fifths, entry and staircase (src/kalshi_moonshot/priskp.py)."""
import math
import random

from kalshi_moonshot import priskp


def setup_function():
    priskp.clear()


def test_the_bucket_is_three_minutes_of_the_market_and_at_most_four():
    assert [priskp.bucket(s) for s in (0, 179, 180, 360, 540, 720, 899)] == [0, 0, 1, 2, 3, 4, 4]


def test_fifths_anomaly_and_unread():
    cut = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]  # 1st, 20th, 40th, 60th, 80th, 99th percentile
    assert [priskp.fifth(cut, x) for x in (1.0, 3.0, 5.0, 7.0, 9.0)] == [1, 2, 3, 4, 5]
    assert priskp.fifth(cut, -0.1) == "anomaly" and priskp.fifth(cut, 10.1) == "anomaly"
    assert priskp.fifth(cut, None) == "unread" and priskp.fifth(None, 5.0) == "unread"


def test_cutoffs_need_a_hundred_readings_a_bucket():
    xs = [float(i) for i in range(100)]
    got = priskp.cutoffs({("KXBTC15M", 2, "cushion"): xs, ("KXBTC15M", 3, "cushion"): xs[:99]})
    assert list(got) == ["KXBTC15M|2|cushion"] and got["KXBTC15M|2|cushion"] == [1.0, 20.0, 40.0, 60.0, 80.0, 99.0]


def test_a_check_with_no_history_is_unread_not_a_pass():
    assert set(priskp.fifths("KXBTC15M", 300.0, {c: 1.0 for c in priskp.CHECKS}).values()) == {"unread"}
    cut = {f"KXBTC15M|1|{c}": [0.0, 2.0, 4.0, 6.0, 8.0, 10.0] for c in priskp.CHECKS}
    assert priskp.load(cut) == 5 and priskp.loaded() == 5
    st = priskp.fifths("KXBTC15M", 200.0, {c: 9.0 for c in priskp.CHECKS})
    assert set(st.values()) == {5} and priskp.candidate(st)
    st["trend"] = 5.0
    assert not priskp.candidate({**st, "trend": 3}) and not priskp.candidate({**st, "cushion": 4})
    assert not priskp.candidate({**st, "price": "unread"})


def test_a_side_must_be_a_candidate_three_readings_in_a_row():
    out = [priskp.confirmed("T", "yes", True, t)[0] for t in (0.0, 10.0, 20.0)]
    assert out == [False, False, True]
    assert priskp.confirmed("T", "yes", False, 30.0) == (False, 0)  # one miss starts the count again
    assert [priskp.confirmed("T", "yes", True, t)[0] for t in (40.0, 50.0, 60.0)] == [False, False, True]


def test_asking_on_every_tick_counts_one_reading_per_ten_seconds():
    for t in (0.0, 1.0, 2.0, 3.0, 9.0):
        assert priskp.confirmed("T", "no", True, t) == (False, 1)
    assert priskp.confirmed("T", "no", True, 10.0) == (False, 2)
    assert priskp.confirmed("T", "no", True, 20.5) == (True, 3)


def test_a_gap_of_readings_starts_the_count_again():
    priskp.confirmed("T", "yes", True, 0.0), priskp.confirmed("T", "yes", True, 10.0)
    assert priskp.confirmed("T", "yes", True, 60.0) == (False, 1)


def test_the_bid_unit_scales_changes_to_a_minute():
    rows = [(10.0 * i, 0.50 + (0.01 if i % 2 else 0.0)) for i in range(20)]  # a 1c change every 10 s
    u = priskp.bid_unit(rows, 190.0)
    assert math.isclose(u, 0.01 * math.sqrt(6.0), rel_tol=1e-9)
    assert priskp.bid_unit(rows[:3], 20.0) == 0.0  # too few changes


def test_the_staircase_sells_under_the_last_swing_low_once_ahead():
    s = priskp.Staircase()
    u, left = 0.02, 600.0
    rev = max(0.01, 1.0 * u * math.sqrt(left / 60.0))  # 0.063
    seq = [(0.60, 0.02), (0.75, 0.10), (0.75 - rev - 0.001, 0.05),  # a swing high confirmed at 0.75
           (0.66, 0.0), (0.66 + rev + 0.001, 0.05)]  # a bounce of rev off the low confirms the swing low at 0.66
    assert [s.step(b, n, u, left) for b, n in seq] == [False] * 5
    assert s.swing_low == 0.66 and s.ahead
    assert s.step(0.70, 0.04, u, left) is False  # above the swing low
    assert s.step(0.659, -0.01, u, left) is True  # under it: sold, here at a small loss against the entry


def test_a_bet_never_ahead_is_not_sold_by_the_staircase():
    s = priskp.Staircase()
    u, left = 0.02, 600.0
    rev = max(0.01, u * math.sqrt(left / 60.0))
    for b in (0.40, 0.50, 0.50 - rev - 0.001, 0.30, 0.30 + rev + 0.001, 0.20):
        assert s.step(b, -0.05, u, left) is False


def _market(seed=1, n=4300):
    rnd = random.Random(seed)
    T, V, v = [], [], 100.0
    for i in range(n):
        v *= math.exp(rnd.gauss(0, 0.0002))
        T.append(float(i)); V.append(v)
    return T, V


def test_side_readings_are_mirrored_between_the_sides():
    T, V = _market()
    opened = 3500.0
    now, close = opened + 360.0, opened + 900.0
    strike = V[int(opened)]
    rnd = random.Random(2)
    quotes, m = [], 0.55
    for i in range(int(now - opened) + 90):
        m = min(0.95, max(0.05, m + rnd.gauss(0, 0.004)))
        quotes.append((opened - 60.0 + i, m - 0.01, m + 0.01))
    got = priskp.side_readings(T, V, quotes, opened, now, strike, close)
    assert got is not None
    y, n = got["yes"], got["no"]
    assert math.isclose(y["cushion"], -n["cushion"]) and math.isclose(y["trend"], -n["trend"])
    assert math.isclose(y["history"] + n["history"], 1.0)
    assert y["price"] is not None and math.isclose(y["price"], -n["price"])
    assert y["pricedip"] <= 0.0 and n["pricedip"] <= 0.0


def test_side_readings_without_enough_index_history_are_none():
    T, V = _market(n=600)
    assert priskp.side_readings(T, V, [], 100.0, 500.0, V[100], 1000.0) is None
