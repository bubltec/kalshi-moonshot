import math

from kalshi_moonshot import accumulator


def test_score_is_the_add_on_net_per_dollar_after_the_fee():
    sig = {"ask": 0.90, "side": "yes"}
    win = accumulator.score_signal(sig, "yes")
    lose = accumulator.score_signal(sig, "no")
    assert lose < -1.0 and 0.0 < win < 0.12  # a loss is the whole add-on plus the fee, a win about 10c less the fee
    assert math.isclose(accumulator.score_signal({"ask": 0.9, "side": "no"}, "no"), win)


def test_picks_follow_the_variants():
    base = {"s13": 0.6, "s55": 0.7, "z": 1.4}
    assert accumulator.picks(base) == {"price only": True, "slopes 13m+55m >= 0.5": True, "z >= 1": True, "slopes and z": True}
    low = accumulator.picks({"s13": 0.6, "s55": 0.1, "z": 2.0})
    assert not low["slopes 13m+55m >= 0.5"] and low["z >= 1"] and not low["slopes and z"]


def test_readings_need_55_minutes_of_ticks():
    ts = [1000.0 + i for i in range(600)]
    vs = [100.0 + 0.001 * i for i in range(600)]
    assert accumulator.readings(ts, vs, 100.0, 1, 1600.0, 90.0) is None
