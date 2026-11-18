"""A sale is written on the side that was sold, not on the lane's other-side row on the same market.

2026-11-18, HYPE 03:30 on the paper lane crypto_risk_score: NO was bought at 0.87, the lane then logged YES at 0.79 unstaked (it still held NO), and 40 s later the NO sale at 0.17 was written on both rows. User: "I think it's fine if we look at both as long as we trace it properly"."""
import pytest

from kalshi_moonshot import config, runner
from kalshi_moonshot.store import Store

T = "KXHYPE15M-1"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def bet(store, side, ask, stake, ts, lane="paper_lane"):
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, T, "bet", "observe", ask, side, lane, 5.0, stake, "2099-01-01T00:00:00Z"))
    store.db.commit()
    store.touch("bets")


def rows(store):
    return {r["side"]: r for r in store.db.execute("SELECT * FROM decisions ORDER BY id").fetchall()}


def test_a_sale_with_a_side_leaves_the_other_sides_row_open(store):
    bet(store, "no", 0.87, 5.0, 1.0)
    bet(store, "yes", 0.79, None, 2.0)
    store.mark_exited(T, 0.17, "curve risk exit", "paper_lane", side="no")
    got = rows(store)
    assert got["no"]["exit_price"] == 0.17
    assert got["yes"]["exit_price"] is None and got["yes"]["exit_ts"] is None
    assert [r["side"] for r in store.open_bets()] == ["yes"]  # the other side is now the lane's open row on the market, watched on its own prices


def test_a_same_side_repeat_takes_the_same_sale(store):
    bet(store, "no", 0.87, 5.0, 1.0)
    bet(store, "no", 0.85, None, 2.0)
    store.mark_exited(T, 0.17, "curve risk exit", "paper_lane", side="no")
    assert store.open_bets() == []


def test_a_sale_with_no_side_named_closes_every_row_as_before(store):
    bet(store, "no", 0.87, 5.0, 1.0)
    bet(store, "yes", 0.79, None, 2.0)
    store.mark_exited(T, 0.17, "legacy call", "paper_lane")
    assert store.open_bets() == []


def test_a_row_with_no_side_counts_as_yes(store):
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, lane, contracts, stake_usd, close_time) VALUES (1.0, ?, 'bet', 'observe', 0.3, 'paper_lane', 3, 0.9, '2099-01-01T00:00:00Z')", (T,))
    store.db.commit()
    store.touch("bets")
    store.mark_exited(T, 0.5, "sale", "paper_lane", side="no")
    assert len(store.open_bets()) == 1
    store.mark_exited(T, 0.5, "sale", "paper_lane", side="yes")
    assert store.open_bets() == []


def test_the_paper_sale_path_closes_each_side_at_its_own_price(store, monkeypatch):
    """Through runner.exit_position, the way the watcher sells: NO at its bid, then YES at its own."""
    monkeypatch.setattr(runner.factors, "snapshot_now", lambda *a, **k: "")
    s = config.load()
    bet(store, "no", 0.87, 5.0, 1.0)
    bet(store, "yes", 0.79, None, 2.0)
    first = store.open_bets()[0]
    assert first["side"] == "no"
    assert "paper-sold" in runner.exit_position(s, None, store, first, 0.17, "curve risk exit: test")
    got = rows(store)
    assert got["no"]["exit_price"] == 0.17 and got["yes"]["exit_price"] is None
    second = store.open_bets()[0]
    assert second["side"] == "yes"
    assert "paper-sold" in runner.exit_position(s, None, store, second, 0.71, "curve risk exit: test")
    got = rows(store)
    assert got["yes"]["exit_price"] == 0.71 and got["no"]["exit_price"] == 0.17
    assert store.open_bets() == []
