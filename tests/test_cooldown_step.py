"""The cooldown buy step with its two waits (buychain.cooldown_step; lane settings rebuy_opposite_after_gain_seconds and reentry_cooldown_seconds, set in a buy module).

2026-11-19, user: "put both into one buy step, cooldown. After a sale, the same side waits N seconds (15 on crypto_risk_score, 120 on the commodity lanes). A sale at a loss blocks the market for reentry_cooldown_seconds. The opposite side goes straight through the other steps after a gain." The HYPE case of 2026-11-08 (sold YES at 0.67 for a gain, bought it again 94 s later at 0.67, lost $2.22) is the reason for the first wait."""
import time

from kalshi_moonshot import buychain, config, lanes
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

TICKER = "KXBTC15M-30JAN010015-15"
NOW = 1_000_000.0


def spec(**kw):
    return lanes.LaneSpec("x", "ladder", "x", (0.60, 0.92), (0, 1), 0.05, 0.0, sides=("yes", "no"), **kw)


def shot(side):
    return LongShot(TICKER, "t", "", 0.69, 0.70, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="x", series="KXBTC15M", side=side, event_ticker="KXBTC15M-30JAN010015")


def store_with(tmp_path, monkeypatch, sales):
    """A store whose lane `x` sold this market: sales = [(side, paid, sold, seconds before NOW)]."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    st = Store(config.load().db_path)
    for side, paid, sold, ago in sales:
        st.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, exit_price, exit_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (NOW - ago - 100, TICKER, "bet", "trade", paid, side, "x", 5.0, 3.0, sold, NOW - ago))
    st.db.commit()
    return st


def ask(sp, side, st):
    return buychain.STEPS["cooldown"](buychain.Ctx(config.load(), sp, p_yes=0.9, store=st, now=NOW), shot(side))


def test_unit_after_a_gain_the_same_side_waits_its_rebuy_wait_and_the_other_side_goes_through(tmp_path, monkeypatch):
    st = store_with(tmp_path, monkeypatch, [("yes", 0.67, 0.80, 10.0)])  # YES bought 0.67, sold 0.80 ten seconds ago: a gain
    sp = spec(rebuy_opposite_after_gain_seconds=15.0, reentry_cooldown_seconds=90.0)
    got = ask(sp, "yes", st)
    assert got.ok is False and "5s more" in got.why and "gain" in got.why
    other = ask(sp, "no", st)
    assert other.ok is True and "other side" in other.why  # even inside the 90 s reentry cooldown
    st2 = store_with(tmp_path / "late", monkeypatch, [("yes", 0.67, 0.80, 20.0)])
    assert ask(sp, "yes", st2).ok is True  # 20 s ago: past the 15 s wait


def test_unit_a_sale_at_a_loss_blocks_the_market_for_the_reentry_cooldown_either_side(tmp_path, monkeypatch):
    st = store_with(tmp_path, monkeypatch, [("yes", 0.70, 0.40, 30.0)])  # a loss, 30 s ago
    sp = spec(rebuy_opposite_after_gain_seconds=15.0, reentry_cooldown_seconds=90.0)
    assert ask(sp, "yes", st).ok is False and ask(sp, "no", st).ok is False
    assert ask(spec(rebuy_opposite_after_gain_seconds=15.0, reentry_cooldown_seconds=20.0), "yes", st).ok is True  # past its wait
    assert ask(spec(rebuy_opposite_after_gain_seconds=15.0), "yes", st).ok is True  # a lane with a rebuy wait only: a loss sets no limit


def test_unit_a_lane_with_only_a_reentry_cooldown_blocks_the_market_after_a_gain_too(tmp_path, monkeypatch):
    st = store_with(tmp_path, monkeypatch, [("yes", 0.67, 0.80, 10.0)])
    sp = spec(reentry_cooldown_seconds=90.0)
    assert ask(sp, "yes", st).ok is False and ask(sp, "no", st).ok is False


def test_unit_the_latest_sale_decides_a_later_loss_after_an_earlier_gain_blocks_the_market(tmp_path, monkeypatch):
    st = store_with(tmp_path, monkeypatch, [("yes", 0.67, 0.80, 60.0), ("no", 0.60, 0.30, 5.0)])  # a gain on YES, then a loss on NO
    sp = spec(rebuy_opposite_after_gain_seconds=15.0, reentry_cooldown_seconds=90.0)
    assert ask(sp, "yes", st).ok is False and ask(sp, "no", st).ok is False  # the latest sale was a loss: the market waits


def test_unit_a_lane_that_sets_no_wait_gets_a_no_and_a_market_never_sold_gets_a_yes(tmp_path, monkeypatch):
    st = store_with(tmp_path, monkeypatch, [])
    assert ask(spec(), "yes", st).ok is False
    assert ask(spec(rebuy_opposite_after_gain_seconds=15.0, reentry_cooldown_seconds=90.0), "yes", st).ok is True
