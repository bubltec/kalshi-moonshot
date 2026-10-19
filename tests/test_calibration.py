import pytest

from kalshi_moonshot.store import Store


def settle(store, ticker, ask, model_p, outcome, lane="moonshot", category="Politics"):
    store.add_decision(
        ticker=ticker, title=f"Will {ticker} happen", bid=ask, ask=ask, model_p=model_p,
        edge=0.0, action="pass", mode="observe", lane=lane, category=category,
    )
    store.set_outcome(ticker, outcome)


def test_no_record_passes_the_estimate_through(tmp_path):
    cal = Store(tmp_path / "m.db").calibrator.adjust("moonshot", "Politics", 0.05, 0.30)
    assert cal.p == 0.30
    assert cal.weight == 1.0
    assert cal.n == 0


def test_a_model_that_loses_to_the_market_loses_its_vote(tmp_path):
    store = Store(tmp_path / "m.db")
    for i in range(40):  # model says 0.6 on 5-cent contracts that never pay
        settle(store, f"LOSER{i}", ask=0.05, model_p=0.6, outcome="no")
    cal = store.calibrator.adjust("moonshot", "Politics", 0.05, 0.6)
    assert cal.skill is not None and cal.skill < 0
    assert cal.weight < 0.4
    assert cal.p < 0.3  # pulled hard toward what the record says about that price band
    assert cal.band_p < 0.05  # 40 straight no's push the band below the ask


def test_a_model_that_beats_the_market_keeps_its_vote(tmp_path):
    store = Store(tmp_path / "m.db")
    for i in range(60):
        yes = i % 4 == 0  # 25% really happen; model says 0.25, market priced 0.10
        settle(store, f"GOOD{i}", ask=0.10, model_p=0.25, outcome="yes" if yes else "no")
    cal = store.calibrator.adjust("moonshot", "Politics", 0.10, 0.25)
    assert cal.skill is not None and cal.skill > 0
    assert cal.weight > 0.3  # positive skill is kept and grows with the record
    assert cal.p > 0.15  # and the price band has learned this price is too low


def test_lanes_are_scored_separately(tmp_path):
    store = Store(tmp_path / "m.db")
    for i in range(30):
        settle(store, f"S{i}", ask=0.7, model_p=0.2, outcome="yes", lane="steady")
    assert store.calibrator.adjust("moonshot", "", 0.05, 0.3).n == 0
    assert store.calibrator.adjust("steady", "", 0.7, 0.3).n == 30


def test_cache_follows_new_settlements(tmp_path):
    store = Store(tmp_path / "m.db")
    assert store.calibrator.adjust("moonshot", "", 0.05, 0.3).n == 0
    settle(store, "A", 0.05, 0.3, "no")
    assert store.calibrator.adjust("moonshot", "", 0.05, 0.3).n == 1


@pytest.mark.parametrize("trust", [0.0, 0.5, 1.0])
def test_trust_sets_the_starting_weight(tmp_path, trust):
    store = Store(tmp_path / "m.db")
    settle(store, "A", 0.05, 0.3, "no")
    cal = store.calibrator.adjust("moonshot", "", 0.05, 0.3, trust=trust)
    assert cal.weight == pytest.approx((1 * max(cal.skill or 0, 0) + 20 * trust) / 21, abs=0.2)
