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
    assert set(priskp.fifths("KXBTC15M", 300.0, dict.fromkeys(priskp.CHECKS, 1.0)).values()) == {"unread"}
    cut = {f"KXBTC15M|1|{c}": [0.0, 2.0, 4.0, 6.0, 8.0, 10.0] for c in priskp.CHECKS}
    assert priskp.load(cut) == 5 and priskp.loaded() == 5
    st = priskp.fifths("KXBTC15M", 200.0, dict.fromkeys(priskp.CHECKS, 9.0))
    assert set(st.values()) == {5} and priskp.candidate(st)
    st["trend"] = 5.0
    assert not priskp.candidate({**st, "trend": 3}) and not priskp.candidate({**st, "cushion": 4})
    assert not priskp.candidate({**st, "price": "unread"})


def test_a_side_must_be_a_candidate_three_readings_in_a_row():
    E = priskp.EVERY
    out = [priskp.confirmed("T", "yes", True, t * E, 3)[0] for t in (0, 1, 2)]
    assert out == [False, False, True]
    assert priskp.confirmed("T", "yes", False, 3 * E, 3) == (False, 0)  # one miss starts the count again
    assert [priskp.confirmed("T", "yes", True, t * E, 3)[0] for t in (4, 5, 6)] == [False, False, True]


def test_asking_on_every_tick_counts_one_reading_per_every_seconds():
    E = priskp.EVERY
    for t in (0.0, 0.2 * E, 0.4 * E, 0.8 * E):
        assert priskp.confirmed("T", "no", True, t, 3) == (False, 1)
    assert priskp.confirmed("T", "no", True, E, 3) == (False, 2)
    assert priskp.confirmed("T", "no", True, 2.1 * E, 3) == (True, 3)


def test_a_gap_of_readings_starts_the_count_again():
    E = priskp.EVERY
    priskp.confirmed("T", "yes", True, 0.0, 3), priskp.confirmed("T", "yes", True, E, 3)
    assert priskp.confirmed("T", "yes", True, 6 * E, 3) == (False, 1)











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


def test_the_note_is_properties_the_bot_can_read():
    ok = dict.fromkeys(priskp.CHECKS, 4) | {"cushion": 5}
    n = priskp.note("yes", ok, 3, 5.0, 3)
    assert (n["verb"], n["why"], n["failing"], n["cushion_fifth"], n["confirm"], n["need"]) == ("buy", "pass", [], 5, 3, 3)
    assert priskp.note("no", ok, 2, 5.0, 3)["verb"] == "wait" and priskp.note("no", ok, 2, 5.0, 3)["why"] == "confirming"
    assert priskp.note("yes", ok | {"cushion": 4}, 3, 5.0, 3)["why"] == "cushion_not_strong"
    r = priskp.note("yes", ok | {"trend": 2, "price": "unread"}, 3, 5.0, 3)
    assert (r["verb"], r["why"], r["failing"]) == ("refuse", "unread", ["trend", "price"])
    assert priskp.note("yes", ok, 3, 1.5, 3)["why"] == "window" and priskp.note("yes", ok, 3, 14.5, 3)["verb"] == "wait"


def test_a_note_written_as_yaml_like_lines_reads_back_the_same():
    ok = dict.fromkeys(priskp.CHECKS, 4) | {"cushion": 5, "trend": "anomaly"}
    n = priskp.note("yes", ok, 1, 7.25, 3)
    text = priskp.render(n)
    assert "verb: refuse" in text and "failing: [trend]" in text and "trend_fifth: anomaly" in text
    assert priskp.parse(text) == n


def _steps(levels, per=60, hold_noise=0.0001, seed=3):
    """A spot made of flat stretches at the given levels (index units), `per` seconds each at 1 tick a second, with a little noise."""
    rnd = random.Random(seed)
    T, V = [], []
    for _k, lv in enumerate(levels):
        for _i in range(per):
            T.append(float(len(T)))
            V.append(lv * (1.0 + rnd.gauss(0, hold_noise)))
    return T, V


def test_a_step_that_holds_for_three_minutes_is_a_shift():
    T, V = _steps([100.0] * 30 + [97.0] * 30)  # an hour: 30 minutes at 100, then a step down to 97 that holds
    got = priskp.shifts(T, V, T[-1], u=0.05)
    assert len(got) == 1 and got[0]["dir"] == -1.0
    assert 28 * 60 < got[0]["ago"] < 32 * 60 and abs(got[0]["step"] + 3.0) < 0.1 and got[0]["held"] > 0.9


def test_a_spike_that_comes_back_is_not_a_shift():
    T, V = _steps([100.0] * 25 + [104.0] * 2 + [100.0] * 33)
    assert priskp.shifts(T, V, T[-1], u=0.05) == []


def test_a_step_still_inside_its_hold_is_not_confirmed_yet():
    T, V = _steps([100.0] * 58 + [97.0] * 2)
    assert priskp.shifts(T, V, T[-1], u=0.05) == []


def test_two_shifts_in_an_hour_are_both_found_newest_last():
    T, V = _steps([100.0] * 15 + [103.0] * 15 + [99.0] * 30)
    got = priskp.shifts(T, V, T[-1], u=0.05)
    assert [g["dir"] for g in got] == [1.0, -1.0] and got[0]["ago"] > got[1]["ago"]


def test_a_move_inside_the_noise_is_not_a_shift():
    T, V = _steps([100.0] * 30 + [100.02] * 30)
    assert priskp.shifts(T, V, T[-1], u=0.05) == []


def test_a_shift_out_of_cushion_blocks_the_side_it_hurt_against_the_current_target():
    down = {"before": 105.0, "after": 99.0}  # a step down from above the strike to under it
    assert priskp.out_of_cushion(down, 100.0, "yes") is True    # YES lost its cushion
    assert priskp.out_of_cushion(down, 100.0, "no") is False    # NO gained one
    inside = {"before": 108.0, "after": 103.0}  # a step down that stayed above the strike
    assert priskp.out_of_cushion(inside, 100.0, "yes") is False
    up = {"before": 95.0, "after": 102.0}
    assert priskp.out_of_cushion(up, 100.0, "no") is True and priskp.out_of_cushion(up, 100.0, "yes") is False
    assert priskp.out_of_cushion(down, 110.0, "yes") is False   # against another target the same step reads differently: it started under that strike
