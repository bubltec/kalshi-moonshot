import math
from types import SimpleNamespace

import pytest

from kalshi_moonshot import config, lanes, scale, sizing
from kalshi_moonshot.store import Store


def spec(**kw):
    base = {"scale_min_n": 40, "scale_z": 2.0, "scale_kelly": 0.25, "scale_max_mult": 3.0}
    return SimpleNamespace(**{**base, **kw})


def test_a_lane_below_the_bar_keeps_its_base_stake():
    sp = spec()
    for rec in (scale.Record(10, 0.30, 0.8), scale.Record(60, 0.02, 0.9), scale.Record(60, -0.1, 0.9)):  # too few bets / not clear of zero / losing
        mult, why = scale.multiplier(rec, sp, 100.0, 2.0)
        assert mult == 1.0 and "record:" in why


def test_the_stake_grows_with_the_lower_bound_and_stops_at_the_cap():
    sp = spec()
    weak = scale.multiplier(scale.Record(100, 0.12, 0.7), sp, 200.0, 2.0)[0]  # lower bound +0.12 - 2*0.07 = -0.02: not clear
    some = scale.multiplier(scale.Record(200, 0.12, 0.7), sp, 200.0, 2.0)[0]  # lower bound +0.12 - 2*0.0495 = +0.021
    more = scale.multiplier(scale.Record(900, 0.20, 0.7), sp, 200.0, 2.0)[0]
    huge = scale.multiplier(scale.Record(5000, 0.40, 0.5), sp, 200.0, 2.0)[0]
    assert weak == 1.0 and 1.0 < some < more <= 3.0 and huge == 3.0
    lo = 0.12 - 2.0 * 0.7 / math.sqrt(200)
    assert some == pytest.approx(0.25 * lo / 0.49 * 200.0 / 2.0)  # a quarter of the Kelly stake the lower bound alone justifies


def _bets(store, n, start=0, slots=None):
    from datetime import UTC, datetime, timedelta

    base = datetime(2099, 1, 1, tzinfo=UTC)
    for j, i in enumerate(range(start, start + n)):  # $2 bets that win 60% of the time at a 2x payout: +20% on stake
        won = (i % 10) < 6
        slot = j if slots is None else slots[j % len(slots)]  # each bet its own 15-minute slot, unless told to share
        close = (base + timedelta(minutes=15 * slot)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.db.execute(
            "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, outcome, close_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (1000.0 + i, f"KXBTC15M-{i}", "bet", "observe", 0.5, "yes", "proving", 4, 2.0, "yes" if won else "no", close))
    store.db.commit()


def test_bets_that_close_together_count_as_one_slot_of_evidence(tmp_path):
    store = Store(tmp_path / "m.db")
    _bets(store, 450, slots=[0])  # 450 bets, every one closing in the same 15-minute slot
    rec = scale.record(store, ("proving",))
    assert rec.n == 450 and rec.n_eff == pytest.approx(1.0)  # one slot is one independent observation
    _bets(store, 0)  # nothing more
    store2 = Store(tmp_path / "m2.db")
    _bets(store2, 450, slots=list(range(45)))  # the same bets spread over 45 slots
    assert scale.record(store2, ("proving",)).n_eff == pytest.approx(45.0)


def test_the_record_comes_from_the_lanes_finished_bets_and_a_small_one_does_not_earn_growth(tmp_path):
    store = Store(tmp_path / "m.db")
    _bets(store, 50)
    rec = scale.record(store, ("proving",))
    assert rec.n == 50 and rec.mean == pytest.approx(0.2)
    assert scale.record(store, ("other",)).n == 0
    sp = lanes.LaneSpec(name="proving", estimator="ladder", label="proving", max_spread=0.05, min_volume=0.0, price=(0.1, 0.9), hours=(0, 1),
                        scale=True, fixed_stake=2.0, scale_min_n=40)
    small = scale.for_lane(store, sp, 100.0)
    assert small.mult == 1.0 and "not above zero" in small.note  # +20% over 50 bets: the lower bound is still under zero
    assert scale.for_lane(store, lanes.LaneSpec(**{**sp.__dict__, "scale": False}), 100.0).mult == 1.0  # off means off


def test_the_same_rate_over_many_more_bets_earns_a_bigger_stake(tmp_path):
    store = Store(tmp_path / "m.db")
    _bets(store, 450)
    sp = lanes.LaneSpec(name="proving", estimator="ladder", label="proving", max_spread=0.05, min_volume=0.0, price=(0.1, 0.9), hours=(0, 1),
                        scale=True, fixed_stake=2.0, scale_min_n=40)
    got = scale.for_lane(store, sp, 100.0, now=1.0e12)
    assert got.record.n == 450 and got.mult > 1.0 and "stake x" in got.note


def test_sizing_applies_the_multiplier_to_the_stake_and_the_cap_but_not_the_hard_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_USD_PER_TRADE", "10")
    s = config.load()
    sp = lanes.LaneSpec(name="x", estimator="ladder", label="x", max_spread=0.05, min_volume=0.0, price=(0.1, 0.9), hours=(0, 1), fixed_stake=2.0, max_exposure_pct=0.05, always=True)
    base = sizing.size_bet(sp, s, 0.5, 0.5, 100.0, 100.0, 0.0)
    grown = sizing.size_bet(sp, s, 0.5, 0.5, 100.0, 100.0, 0.0, 2.5)
    assert grown.cost_usd > 2 * base.cost_usd and "x2.50" in grown.why
    capped = sizing.size_bet(sp, s, 0.5, 0.5, 100.0, 100.0, 0.0, 9.0)  # asks for $18, the hard ceiling is $10 a bet
    assert capped.cost_usd <= 10.0 + 1e-9
    room = sizing.size_bet(sp, s, 0.5, 0.5, 100.0, 100.0, 4.0, 1.0)  # $4 open of a $5 cap: little room at the base multiplier
    wider = sizing.size_bet(sp, s, 0.5, 0.5, 100.0, 100.0, 4.0, 2.0)
    assert room.cost_usd < wider.cost_usd


def test_a_small_change_keeps_most_of_the_old_record_and_a_big_one_keeps_little():
    base = {"price": "(0.6, 0.85)", "fixed_stake": "5.0", "stop_guard": "True", "trail_drop": "0.04", "always": "True"}
    tweak = {**base, "trail_drop": "0.05"}  # one number nudged
    flipped = {**base, "stop_guard": "False"}  # one switch
    rewrite = {"price": "(0.1, 0.4)", "fixed_stake": "2.0", "stop_guard": "False", "trail_drop": "0.0", "always": "False"}
    assert scale.similarity(base, base) == pytest.approx(1.0)
    assert scale.similarity(base, tweak) > scale.similarity(base, flipped) > scale.similarity(base, rewrite)
    assert scale.similarity(base, tweak) > 0.9 and scale.similarity(base, rewrite) < 0.3


def test_old_versions_count_by_similarity_and_fade_as_the_new_one_builds(tmp_path):
    import json

    store = Store(tmp_path / "m.db")
    cur_set = {"fixed_stake": "5.0", "stop_guard": "False"}
    old_set = {"fixed_stake": "5.0", "stop_guard": "True"}
    store.note_lane_version("proving", "old", old_set)
    store.note_lane_version("proving", "new", cur_set)
    json.loads(store.lane_versions("proving")[0]["settings"])  # stored as readable settings
    from datetime import UTC, datetime, timedelta

    base = datetime(2099, 1, 1, tzinfo=UTC)
    for i in range(60):  # the old version: a losing record, 60 bets of -50%, each in its own slot
        close = (base + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.db.execute(
            "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, outcome, close_time, lane_ver) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (1000.0 + i, f"OLD-{i}", "bet", "observe", 0.5, "yes", "proving", 4, 2.0, "yes" if i % 4 == 0 else "no", close, "old"))
    store.db.commit()
    current = {"proving": ("new", cur_set)}
    alone = scale.record(store, ("proving",), 0.0, current, 0.5)
    assert alone.n == 60 and alone.n_eff == pytest.approx(60.0)  # no new bets yet: only the old ones to go on, all at one weight
    weight = scale.weights_for(store, "proving", "new", cur_set, 0.5)
    assert weight["new"] == 1.0 and 0 < weight["old"] <= 0.5
    for i in range(30):  # the new version: 30 winning bets, each in its own slot
        close = (base + timedelta(minutes=15 * (100 + i))).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.db.execute(
            "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, outcome, close_time, lane_ver) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (2000.0 + i, f"NEW-{i}", "bet", "observe", 0.5, "yes", "proving", 4, 2.0, "yes", close, "new"))
    store.db.commit()
    mixed = scale.record(store, ("proving",), 0.0, current, 0.5)
    plain = scale.record(store, ("proving",), 0.0)
    assert mixed.n == 90 and mixed.mean > plain.mean  # the new version's wins outweigh its place in the pile; the old losses are discounted
    assert mixed.n_eff < 90  # and the effective number of bets reflects the discounting
