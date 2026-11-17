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


def test_two_lanes_rating_the_same_markets_each_get_their_own_record(tmp_path):
    store = Store(tmp_path / "m.db")
    for i in range(25):
        for lane, p in (("older", 0.5), ("newer", 0.9)):  # both lanes rate every market; only the earlier one used to be counted
            store.add_decision(ticker=f"M{i}", title="t", bid=0.5, ask=0.5, model_p=p, edge=0.0, action="pass", mode="observe", lane=lane, category="")
        store.set_outcome(f"M{i}", "no")
    assert store.calibrator.adjust("older", "", 0.5, 0.5).n == 25
    newer = store.calibrator.adjust("newer", "", 0.5, 0.9)
    assert newer.n == 25 and newer.skill is not None and newer.skill < 0  # scored against its own answers, found worse than the market
    assert newer.weight < 0.5


# 2026-11-16, user: "Nothing in per tick computations should do a db read. Ever. We can do structured background jobs to build a template for a market but never per tick".
def test_the_served_record_answers_from_memory_and_matches_the_on_demand_one(tmp_path):
    from kalshi_moonshot import calibration

    store = Store(tmp_path / "m.db")
    for i in range(40):
        store.db.execute(
            "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, model_p, category, outcome) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1000.0 + i, f"T{i}", "bet", "paper", 0.60 + 0.01 * (i % 5), "yes", "moonshot", 0.70, "Crypto" if i % 2 else "", "yes" if i % 3 else "no"))
    store.db.commit()
    want = [store.calibrator.adjust("moonshot", cat, ask, 0.7) for cat in ("", "Crypto") for ask in (0.61, 0.63, 0.9)]
    try:
        calibration.rebuild(store)
        seen = []
        inner = store.db

        class Watch:
            def execute(_, sql, *args):
                seen.append(sql)
                return inner.execute(sql, *args)

            def __getattr__(_, name):
                return getattr(inner, name)

        store.db = Watch()
        store.generation += 1  # a bet settled: on demand this would reload every settled decision
        got = [store.calibrator.adjust("moonshot", cat, ask, 0.7) for cat in ("", "Crypto") for ask in (0.61, 0.63, 0.9)]
        assert got == want and seen == []
    finally:
        calibration.stop_serving()


def test_the_served_record_changes_only_when_it_is_rebuilt(tmp_path):
    from kalshi_moonshot import calibration

    store = Store(tmp_path / "m.db")
    try:
        calibration.rebuild(store)
        assert store.calibrator.adjust("moonshot", "", 0.6, 0.7).n == 0
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, model_p, outcome) VALUES (1,'T','bet','paper',0.6,'yes','moonshot',0.7,'yes')")
        store.db.commit()
        assert store.calibrator.adjust("moonshot", "", 0.6, 0.7).n == 0  # not read per call
        calibration.rebuild(store)
        assert store.calibrator.adjust("moonshot", "", 0.6, 0.7).n == 1
    finally:
        calibration.stop_serving()
