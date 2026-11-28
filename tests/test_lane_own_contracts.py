"""A lane manages only its own bets (2026-11-27, ZEC 05:45; user: "lanes are not keeping track of their own bets and logic is bleeding across wave and priskp. Simple fix ... is making sure lanes only manage their own bets").

What happened there: wave_shape held a 0.4-contract YES bet and priskp a 25-contract NO bet on the same market. The orders table was summed per TICKER, so wave_shape's give-up sale was sized at 25 and sent as 25 YES contracts 27 times (the account held none); 20 seconds after priskp bought NO again, the "account holds fewer than the books" check wrote 20.54 contracts off as sold by hand against the YES side, the books then showed no NO contracts and the real NO position was adopted as a hand-placed one and held to zero."""
import pytest

from kalshi_moonshot import config, runner
from kalshi_moonshot.store import Store
from tests.test_risk_exit_flow import CLOSE, TICKER, Client, held, live

WHY = "one rule, salvage: under the price paid, the spot has crossed the target"


def other_lane_holds_no(s, n=25.0, paid=0.60, lane="priskp"):
    """Another lane's NO bet on the same market and its filled buy order."""
    st = Store(s.db_path)
    st.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)", (CLOSE - 400, TICKER, "bet", "trade", paid, "no", lane, n, paid * n, "2030-01-01T00:15:00Z"))
    st.db.execute("INSERT INTO orders (ts, ticker, env, count, price, fill_count, cost_usd, fee_usd, order_id, side, contract) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (CLOSE - 399, TICKER, s.env, n, paid, n, paid * n, 0.0, "o-no", "buy", "no"))
    st.db.commit()
    return st


def test_unit_net_contracts_and_cost_can_be_read_per_side(tmp_path, monkeypatch):
    held(tmp_path, monkeypatch, live=True)  # a YES bet of 4.65 contracts
    s = config.load()
    st = other_lane_holds_no(s)
    assert st.net_contracts(TICKER, s.env) == pytest.approx(4.65 + 25.0)  # the old, whole-ticker reading
    assert st.net_contracts(TICKER, s.env, "yes") == pytest.approx(4.65) and st.net_contracts(TICKER, s.env, "no") == pytest.approx(25.0)
    assert st.net_cost(TICKER, s.env, "no") == pytest.approx(15.0)
    assert st.lane_contracts(TICKER, "risk_only", "yes") == pytest.approx(4.65) and st.lane_contracts(TICKER, "priskp", "no") == pytest.approx(25.0)
    assert st.lane_contracts(TICKER, "priskp", "yes") == 0.0 and st.lane_contracts(TICKER, "risk_only", "no") == 0.0  # a lane holds nothing of the other lane's


def test_integration_a_sale_is_sized_by_its_own_side_and_its_own_bets_and_leaves_the_other_lanes_contracts_alone(tmp_path, monkeypatch):
    """The ZEC 05:45 case: lane A (YES, 4.65) sells while lane B holds 25 NO on the same market and the account holds only the NO."""
    live(monkeypatch)
    c = Client(bid=0.30, account={TICKER: -25.0})  # signed: negative = NO; the account holds no YES
    w = held(tmp_path, monkeypatch, live=True, client=c, extra="sell_chain=['one_rule']")
    s = config.load()
    other_lane_holds_no(s)
    row = next(r for r in Store(s.db_path).open_bets() if r["lane"] == "risk_only")
    runner.exit_position(s, c, Store(s.db_path), row, 0.30, WHY)
    assert all(n <= 4.65 + 1e-9 for n, _ in c.sold), c.sold  # never 25: the other lane's NO count
    st = Store(s.db_path)
    assert st.net_contracts(TICKER, s.env, "no") == pytest.approx(25.0)  # lane B's contracts were not written off as sold by hand
    assert st.db.execute("SELECT COUNT(*) AS n FROM orders WHERE order_id='hand-sale' AND contract='no'").fetchone()["n"] == 0
    assert st.db.execute("SELECT COUNT(*) AS n FROM decisions WHERE lane='priskp' AND exit_ts IS NOT NULL").fetchone()["n"] == 0  # and its bet is still open
    assert w is not None


def test_integration_two_lanes_on_the_same_side_each_sell_only_their_own_contracts(tmp_path, monkeypatch):
    live(monkeypatch)
    c = Client(bid=0.60, account={TICKER: 14.65})  # the account holds both lanes' YES
    held(tmp_path, monkeypatch, live=True, client=c, extra="sell_chain=['one_rule']")  # lane risk_only: YES 4.65
    s = config.load()
    st = Store(s.db_path)
    st.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)", (CLOSE - 300, TICKER, "bet", "trade", 0.6, "yes", "priskp", 10.0, 6.0, "2030-01-01T00:15:00Z"))
    st.db.execute("INSERT INTO orders (ts, ticker, env, count, price, fill_count, cost_usd, fee_usd, order_id, side, contract) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (CLOSE - 299, TICKER, s.env, 10.0, 0.6, 10.0, 6.0, 0.0, "o-b", "buy", "yes"))
    st.db.commit()
    row = next(r for r in Store(s.db_path).open_bets() if r["lane"] == "risk_only")
    line = runner.exit_position(s, c, Store(s.db_path), row, 0.60, WHY)
    assert [n for n, _ in c.sold] == [4.65] and line.startswith("sold 4.65/4.65")  # only lane A's own 4.65 of the 14.65 held
    assert Store(s.db_path).net_contracts(TICKER, s.env, "yes") == pytest.approx(10.0)  # lane B's 10 are untouched
    assert Store(s.db_path).db.execute("SELECT COUNT(*) AS n FROM decisions WHERE lane='priskp' AND exit_ts IS NOT NULL").fetchone()["n"] == 0
    assert Store(s.db_path).db.execute("SELECT COUNT(*) AS n FROM decisions WHERE lane='risk_only' AND exit_ts IS NOT NULL").fetchone()["n"] == 1


def test_unit_a_hand_sale_is_written_off_against_its_own_side_only(tmp_path, monkeypatch):
    live(monkeypatch)
    held(tmp_path, monkeypatch, live=True)
    s = config.load()
    st = other_lane_holds_no(s)
    gone = runner.book_hand_sale(s, st, TICKER, "yes", 0.0, 0.30)  # the account holds no YES: the YES side is written off, 4.65, and nothing more
    assert gone == pytest.approx(4.65)
    assert st.net_contracts(TICKER, s.env, "yes") == pytest.approx(0.0) and st.net_contracts(TICKER, s.env, "no") == pytest.approx(25.0)


def test_a_lane_is_held_back_by_its_own_position_on_a_market_and_not_by_another_lanes(tmp_path, monkeypatch):
    """2026-11-27, user: "It shouldn't block the whole market though. The wave bets are short": risk.check refused every buy on a market the ACCOUNT held, so wave_shape's short bets stopped PriskP buying the same market (511 refusals on 11 markets in 110 minutes). Now a lane is held back by its own open bet on the market only."""
    from kalshi_moonshot import risk

    live(monkeypatch)
    held(tmp_path, monkeypatch, live=True)  # the lane risk_only holds a YES bet on TICKER
    s = config.load()
    monkeypatch.setenv("ALLOW_REAL_MONEY", "true")
    s = config.load()
    st = Store(s.db_path)
    assert risk.check(s, st, TICKER, 1.0, max_trade=5.0).reason == "already holding this market"  # no lane given: the account's rule, as before
    assert risk.check(s, st, TICKER, 1.0, max_trade=5.0, lane="risk_only").reason == "already holding this market"  # its own position blocks it
    assert risk.check(s, st, TICKER, 1.0, max_trade=5.0, lane="wave_shape").reason != "already holding this market"  # another lane's does not


def test_a_lane_may_not_buy_the_other_side_of_a_market_another_lane_holds(tmp_path, monkeypatch):
    """2026-11-27 evening: the wave lane's short bets no longer block PriskP (a lane is held back only by its own position), but Kalshi nets a market's YES and NO contracts, so a lane buying the OTHER side of another lane's open bet cancels contracts against it: BTC 20:00 NO, HYPE 20:15 YES and ZEC 21:30 YES (PriskP, 1.0, 1.2 and 5.0 contracts) were booked 'closed by hand: the account no longer holds these contracts' while the wave lane held the opposite side. The same side is allowed, the other side is refused."""
    from kalshi_moonshot import risk

    live(monkeypatch)
    held(tmp_path, monkeypatch, live=True)  # the lane risk_only holds a YES bet on TICKER
    monkeypatch.setenv("ALLOW_REAL_MONEY", "true")
    s = config.load()
    st = Store(s.db_path)
    other = risk.check(s, st, TICKER, 1.0, max_trade=5.0, lane="wave_shape", side="no")
    assert other.reason == "another lane holds the other side of this market (the exchange nets the two)"
    same = risk.check(s, st, TICKER, 1.0, max_trade=5.0, lane="wave_shape", side="yes")
    assert same.reason != "another lane holds the other side of this market (the exchange nets the two)" and "other side" not in same.reason
    own = risk.check(s, st, TICKER, 1.0, max_trade=5.0, lane="risk_only", side="no")
    assert "other side" not in own.reason  # its own bet is the lane's own business, held back by the holding rule as before
