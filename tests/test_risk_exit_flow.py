"""The risk exit end to end, through the real watcher path (2026-11-13). The first tests of the exit built a Target by hand and so never touched the step that failed live: a lane with only curve_risk_sell set was never given a watched position (takes_profit() was false), so nothing was armed and nothing could sell. These tests start from the lane file and the stored bet, as the bot does: Store row -> refresh() -> quote events -> sale signal."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_moonshot import config, lanes, watcher
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.store import Store

CLOSE = datetime(2030, 1, 1, 0, 15, tzinfo=UTC).timestamp()
TICKER = "KXBTC15M-30JAN010015-15"

LANE = """[lane.risk_only]
estimator='ladder'
sides=['yes']
price=[0.70,0.90]
hours=[0.0,0.26]
timed_suffix='15M'
stop_loss=false
curve_risk_sell=0.5
curve_risk_live={live}
curve_risk_spot_weight=0.0
curve_risk_history=false
curve_risk_confirm={confirm}
curve_risk_stop_hold={hold}
curve_risk_swing={swing}
curve_risk_swing_floor={floor}
"""


class Client:
    """The market reads the watcher makes when it arms a position, and the sale it places."""

    authed = True

    def __init__(self, bid=0.56, fills=None, account=None):
        self.bid = bid
        self.sold = []
        self.fills = fills  # None = every sale fills in full; a number = each sale fills that many
        self.account = account  # None = the account cannot be read; else {ticker: signed contracts (+ YES, - NO)}

    def positions(self):
        if self.account is None:
            from kalshi_moonshot.kalshi import KalshiError

            raise KalshiError("no positions in this test")
        return {"market_positions": [{"ticker": tk, "position_fp": str(n), "market_exposure_dollars": str(abs(n) * 0.5)} for tk, n in self.account.items()]}

    def orderbook(self, ticker):
        return {}

    def market(self, ticker):
        return {"ticker": ticker, "floor_strike": 100.0, "close_time": "2030-01-01T00:15:00Z", "yes_bid_dollars": str(self.bid), "yes_ask_dollars": str(self.bid + 0.01)}

    def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.sold.append((count, price))
        got = count if self.fills is None else min(count, self.fills)
        return {"fill_count": got, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "s"}


def held(tmp_path, monkeypatch, live, client=None, confirm=-1, hold=0, swing=0, floor=0):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(live="true" if live else "false", confirm=confirm, hold=hold, swing=swing, floor=floor))
    s = config.load()
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (CLOSE - 450, TICKER, "bet", "trade", 0.81, "yes", "risk_only", 4.65, 3.77, "2030-01-01T00:15:00Z"))
    store.db.execute(  # the live fill the executor sells against: without a buy order the sale is a paper one
        "INSERT INTO orders (ts, ticker, env, count, price, fill_count, cost_usd, fee_usd, order_id, side, contract) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (CLOSE - 449, TICKER, s.env, 4.65, 0.81, 4.65, 3.77, 0.10, "o1", "buy", "yes"))
    store.db.commit()
    w = watcher.ProfitWatcher(lambda: s, client or Client(), lambda: Store(s.db_path))
    w.refresh()
    return w


def yes_bid(w, bid, left):
    w.on_event({"kind": "quote", "ticker": TICKER, "bid": bid, "ask": round(bid + 0.01, 4), "ts": CLOSE - left})


def signals(w):
    out = []
    while not w._jobs.empty():
        out.append(w._jobs.get_nowait())
    return out


# the platinum bet of 2026-11-13: YES bought at 0.81, the bid slid to 0.56 over ninety seconds
PATH = [0.77, 0.74, 0.72, 0.70, 0.68, 0.66, 0.64, 0.62, 0.60, 0.56]


def test_a_lane_with_only_the_risk_exit_gets_a_watched_position_with_the_exit_armed(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True)
    t = w.targets.get((TICKER, "risk_only"))
    assert t is not None and t.risk_sell == 0.5 and t.risk_live and t.risk_spot_weight == 0.0 and t.close_ts > 0


def test_live_it_sells_a_sliding_bet_at_the_max_loss_and_not_before(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True)
    sold = None
    for k, bid in enumerate(PATH):
        yes_bid(w, bid, 450 - 3 * k)
        out = signals(w)
        if out:
            sold = (bid, out[0].why)
            break
    assert sold is not None, "the bet slid to 0.56 and nothing sold"
    assert sold[0] > 0.55 and sold[1].startswith("curve risk exit (max loss)")
    assert sold[0] <= 0.62  # not on the early dip: at 0.70 the loss is 16% against a limit of a third


def test_the_sale_signal_is_executed_as_an_order_at_a_loss_and_the_bet_closes(tmp_path, monkeypatch):
    """The sell executor vetoes a sale at a loss unless its reason is on a list of exits that cut losses by design; 'curve risk exit' was not, so every loss-cutting sale of the risk exit was declined and no order was placed (11:08 WTI, -$9.65). This test follows the signal all the way to the order."""
    c = Client(bid=0.56)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)  # a live lane: the sale is a real order, not a paper one
    w = held(tmp_path, monkeypatch, live=True, client=c)
    job = None
    for k, bid in enumerate(PATH):
        yes_bid(w, bid, 450 - 3 * k)
        if not w._jobs.empty():
            job = w._jobs.get_nowait()
            break
    assert job is not None
    w._sell(*job)
    assert len(c.sold) == 1, "the signal was raised but no sell order was placed"
    assert len(Store(config.load().db_path).open_bets()) == 0


def test_in_shadow_it_sells_nothing(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=False)
    for k, bid in enumerate(PATH):
        yes_bid(w, bid, 450 - 3 * k)
    assert signals(w) == []


def test_the_lanes_that_trade_live_are_all_watched(tmp_path, monkeypatch):
    """Guards the live config itself: every lane in LIVE_LANES must be one the watcher tracks (takes_profit), or it has no exits at all (the 09:45 break). Reads the Mac's lane file and .env as text only, with settings from a scratch data dir."""
    root = Path(__file__).resolve().parents[1]
    toml, env = root / "data" / "strategies.toml", root / ".env"
    if not toml.exists() or not env.exists():
        pytest.skip("no local lane file or .env")
    live = next((line.split("=", 1)[1].strip().strip('"') for line in env.read_text().splitlines() if line.startswith("LIVE_LANES=")), "")
    names = [n for n in live.split(",") if n]
    if not names:
        pytest.skip("LIVE_LANES is blank")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    specs = lanes.lane_specs_from(config.load(), toml.read_text())
    unwatched = [n for n in names if n in specs and not lanes.takes_profit(specs[n])]
    assert unwatched == [], f"live lanes with no watched exit: {unwatched}"


def seed_perp(monkeypatch, last_over_open):
    """An hour of the Kalshi perp ahead of the 450 s mark: ticks every 20 s around 100.0 with a little noise, ending `last_over_open` (a fraction) over the value at the window open (CLOSE - 900), which is 100.0."""
    fresh = ticks_mod.TickHistory()
    monkeypatch.setattr(ticks_mod, "history", fresh)
    start = CLOSE - 4300.0
    rows = []
    for k in range(int((4300.0 - 100.0) // 20)):
        ts = start + 20.0 * k
        base = 100.0 + (0.02 if k % 2 else -0.02)
        if ts >= CLOSE - 900.0:
            base = 100.0 + (100.0 * last_over_open) * min(1.0, (ts - (CLOSE - 900.0)) / 450.0)
        rows.append((ts, base))
    fresh.warm("KP_BTC", rows, start)
    return fresh


def slide(w):
    for k, bid in enumerate(PATH):
        yes_bid(w, bid, 450 - 3 * k)
    return signals(w)


def test_integration_the_hour_view_keeps_a_bet_the_price_alone_would_sell(tmp_path, monkeypatch):
    """The 12:13 NEAR case: the price dips through the max-loss limit but the perp, against its own last hour, says the side stays on its side: nothing sells."""
    seed_perp(monkeypatch, last_over_open=0.004)  # 40 bp over the open, 7-8 minutes left
    w = held(tmp_path, monkeypatch, live=True, confirm=0.4)
    assert slide(w) == []


def test_integration_the_hour_view_agrees_and_the_sale_goes(tmp_path, monkeypatch):
    seed_perp(monkeypatch, last_over_open=-0.004)  # the perp is 40 bp under the open: a YES bet is losing on every layer
    w = held(tmp_path, monkeypatch, live=True, confirm=0.4)
    out = slide(w)
    assert out and out[0].why.startswith("curve risk exit (max loss)")


def test_integration_with_no_perp_hour_the_price_alone_decides(tmp_path, monkeypatch):
    monkeypatch.setattr(ticks_mod, "history", ticks_mod.TickHistory())
    w = held(tmp_path, monkeypatch, live=True, confirm=0.4)
    assert slide(w) != []


# ---- a position the user sold by hand (2026-11-13: the bot sent 100 sell orders in 77 s for contracts that were gone) ----

def live(monkeypatch):
    from kalshi_moonshot import runner

    monkeypatch.setattr(runner, "HAND_SALE_MIN_AGE", -1e12)  # the fixture's market is in 2030, so its buy order is "in the future": count it as old; the freshness test below sets the real guard back
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")


def first_signal(w):
    for k, bid in enumerate(PATH):
        yes_bid(w, bid, 450 - 3 * k)
        if not w._jobs.empty():
            return w._jobs.get_nowait()
    return None


def test_unit_account_count_reads_the_side_held_and_none_when_it_cannot_read():
    from kalshi_moonshot import runner

    c = Client(account={TICKER: 4.12, "OTHER-1": -3.0})
    assert runner.account_count(c, TICKER, "yes") == 4.12
    assert runner.account_count(c, TICKER, "no") == 0.0  # the account holds the other side
    assert runner.account_count(c, "OTHER-1", "no") == 3.0 and runner.account_count(c, "GONE-1", "yes") == 0.0
    assert runner.account_count(Client(account=None), TICKER, "yes") is None
    assert runner.account_count(object(), TICKER, "yes") is None


def test_unit_a_shortfall_is_written_off_only_when_the_last_order_is_old_enough(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    live(monkeypatch)
    w = held(tmp_path, monkeypatch, live=True)
    s = config.load()
    store = Store(s.db_path)
    assert runner.book_hand_sale(s, store, TICKER, "yes", 4.65, 0.5) == 0.0  # the books agree
    assert runner.book_hand_sale(s, store, TICKER, "yes", 2.0, 0.5) == pytest.approx(2.65)  # the buy order is 449 s before the close of a 2030 market: old
    assert store.net_contracts(TICKER, s.env) == pytest.approx(2.0)
    store.add_order(ticker=TICKER, env=s.env, count=1.0, price=0.5, fill_count=1.0, cost_usd=0.5, fee_usd=0.0, order_id="fresh", contract="yes")
    monkeypatch.setattr(runner, "HAND_SALE_MIN_AGE", 20.0)
    monkeypatch.setattr(store, "last_order_ts", lambda *a: __import__("time").time())
    assert runner.book_hand_sale(s, store, TICKER, "yes", 0.0, 0.5) == 0.0  # an order a moment ago: the account read may trail it
    monkeypatch.setattr(store, "last_order_ts", lambda *a: __import__("time").time() - 60.0)
    assert runner.book_hand_sale(s, store, TICKER, "yes", 0.0, 0.5) == pytest.approx(3.0)  # a minute old: trusted
    assert w is not None


def test_integration_the_sale_sells_only_what_the_account_still_holds(tmp_path, monkeypatch):
    live(monkeypatch)
    c = Client(bid=0.56, account={TICKER: 3.0})  # the user sold 1.65 of the 4.65 by hand
    w = held(tmp_path, monkeypatch, live=True, client=c)
    job = first_signal(w)
    assert job is not None
    w._sell(*job)
    assert [n for n, _ in c.sold] == [3.0]  # one order, for the three contracts that are there
    assert len(Store(config.load().db_path).open_bets()) == 0


def test_integration_a_bet_sold_entirely_by_hand_sends_no_order_and_is_closed(tmp_path, monkeypatch):
    live(monkeypatch)
    c = Client(bid=0.56, account={})  # nothing of it left on the account
    w = held(tmp_path, monkeypatch, live=True, client=c)
    job = first_signal(w)
    w._sell(*job)
    s = config.load()
    store = Store(s.db_path)
    assert c.sold == [] and len(store.open_bets()) == 0
    assert TICKER not in store.open_position_tickers(s.env)  # the lane may buy this market again
    row = store.db.execute("SELECT exit_reason FROM decisions WHERE ticker=?", (TICKER,)).fetchone()
    assert "closed by hand" in row["exit_reason"]


def test_integration_when_the_account_cannot_be_read_the_sale_goes_by_the_books(tmp_path, monkeypatch):
    live(monkeypatch)
    c = Client(bid=0.56, account=None)
    w = held(tmp_path, monkeypatch, live=True, client=c)
    w._sell(*first_signal(w))
    assert [n for n, _ in c.sold] == [4.65]


def test_integration_a_sale_that_fills_nothing_backs_off_instead_of_firing_every_second(tmp_path, monkeypatch):
    live(monkeypatch)
    monkeypatch.setattr("kalshi_moonshot.runner.time.sleep", lambda x: None)
    c = Client(bid=0.30, fills=0.0, account=None)  # every order comes back unfilled
    w = held(tmp_path, monkeypatch, live=True, client=c)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(watcher.time, "time", lambda: clock["now"])
    signals_sent = 0
    for k in range(77):  # 77 seconds of quotes at a bid past the max loss, as on the BTC bet
        clock["now"] += 1.0
        yes_bid(w, 0.30, 400 - k)
        while not w._jobs.empty():
            w._sell(*w._jobs.get_nowait())
            signals_sent += 1
    assert 1 <= signals_sent <= 7, signals_sent  # 2, 4, 8, 16, 32 s apart: a handful of tries, not seventy-seven
    assert w.targets[(TICKER, "risk_only")].sell_misses == signals_sent


def test_integration_a_hand_sale_seen_on_two_account_reads_closes_the_bet_so_the_lane_can_buy_again(tmp_path, monkeypatch):
    from kalshi_moonshot import manual, risk

    live(monkeypatch)
    monkeypatch.setattr(manual, "POLL_EVERY", 0.0)  # every call reads the account
    c = Client(bid=0.90, account={TICKER: 4.65})
    w = held(tmp_path, monkeypatch, live=True, client=c)
    s = config.load()
    store = Store(s.db_path)
    yes_bid(w, 0.90, 420)
    tick = {"now": CLOSE - 400.0}  # the market is open; each call is a second later, so each account read is a later one

    def clock():
        tick["now"] += 1.0
        return tick["now"]

    monkeypatch.setattr(watcher.time, "time", clock)
    assert w.reconcile_hand_sales(s, store) == [] and len(store.open_bets()) == 1  # the account holds it all
    c.account = {}  # the user cashes out by hand
    assert w.reconcile_hand_sales(s, store) == [] and len(store.open_bets()) == 1  # seen once: not acted on yet
    out = w.reconcile_hand_sales(s, store)  # a second, later read agrees
    assert len(out) == 1 and "closed by hand" in out[0] and len(store.open_bets()) == 0
    assert c.sold == []
    monkeypatch.setenv("ALLOW_REAL_MONEY", "true")
    assert risk.check(config.load(), store, TICKER, 1.0, max_trade=5.0).reason != "already holding this market"


# ---- the perp is silent: the long layer falls back to the lane's own spot index (2026-11-13, Brent sold on a 9-second dip with "long layer n/a") ----

def seed_spot_only(monkeypatch, last_over_strike):
    """An hour of the lane's spot index (BRTI for KXBTC15M, armed against the strike 100.0) and a Kalshi perp with ONE old reading, as a futures reference is outside its hours."""
    fresh = ticks_mod.TickHistory()
    monkeypatch.setattr(ticks_mod, "history", fresh)
    start = CLOSE - 4300.0
    rows = [(start + 20.0 * k, 100.0 * (1.0 + last_over_strike) + (0.02 if k % 2 else -0.02)) for k in range(int((4300.0 - 100.0) // 20))]
    fresh.warm("BRTI", rows, start)
    fresh.warm("KP_BTC", [(CLOSE - 2000.0, 100.0)], CLOSE - 2000.0)
    return fresh


def test_integration_a_silent_perp_falls_back_to_the_spot_index_and_holds_a_dip(tmp_path, monkeypatch):
    seed_spot_only(monkeypatch, last_over_strike=0.004)  # the spot sits 40 bp over the strike: a YES bet is fine on the long view
    w = held(tmp_path, monkeypatch, live=True, confirm=0.4)
    t = w.targets[(TICKER, "risk_only")]
    assert t.shock_index == "BRTI" and t.shock_ref == 100.0
    assert slide(w) == []  # the price dips through the max loss; the spot's hour says hold
    assert t.risk_long is not None and t.risk_long > 0.9 and t.risk_long_fallback


def test_integration_a_silent_perp_and_a_spot_under_the_strike_still_sells(tmp_path, monkeypatch):
    seed_spot_only(monkeypatch, last_over_strike=-0.004)
    w = held(tmp_path, monkeypatch, live=True, confirm=0.4)
    out = slide(w)
    assert out and out[0].why.startswith("curve risk exit (max loss)")


def test_integration_a_settled_market_leaving_the_account_is_not_a_hand_sale(tmp_path, monkeypatch):
    """After the close the position disappears from the account because it settled. The check must leave that bet to the settlement, not close it at a price of 0."""
    from kalshi_moonshot import manual

    live(monkeypatch)
    monkeypatch.setattr(manual, "POLL_EVERY", 0.0)
    c = Client(bid=0.90, account={TICKER: 4.65})
    w = held(tmp_path, monkeypatch, live=True, client=c)
    s = config.load()
    store = Store(s.db_path)
    yes_bid(w, 0.90, 420)
    clock = {"now": CLOSE - 400.0}
    monkeypatch.setattr(watcher.time, "time", lambda: clock["now"])
    assert w.reconcile_hand_sales(s, store) == []
    c.account = {}  # settled: gone from the account
    clock["now"] = CLOSE + 30.0
    assert w.reconcile_hand_sales(s, store) == [] and w.reconcile_hand_sales(s, store) == []
    assert len(store.open_bets()) == 1 and store.net_contracts(TICKER, s.env) == pytest.approx(4.65)
    clock["now"] = CLOSE - 5.0  # the last seconds before the close: not acted on either
    assert w.reconcile_hand_sales(s, store) == [] and w.reconcile_hand_sales(s, store) == []


# ---- the stop waits a share of the time left (2026-11-14, user: "A 57c bet should be able to deal with a dip for a moment with 5 mins remaining") ----
def test_integration_the_stop_waits_half_the_time_left_and_then_sells(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, hold=0.5)
    sold_at = None
    for k in range(0, 150):  # the bid drops to 0.55 at 450 s left and stays there: past the stop from the first quote
        left = 450 - 3 * k
        yes_bid(w, 0.55, left)
        if signals(w):
            sold_at = left
            break
    assert sold_at is not None, "a bid that stayed under the stop for the whole wait was never sold"
    assert 295 <= sold_at <= 310  # the first hit at 450 s left, then 0.5 x the time left (shrinking): the two meet near 300 s left


def test_integration_a_dip_that_recovers_inside_the_wait_does_not_sell_and_the_clock_restarts(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, hold=0.5)
    for k in range(0, 20):  # 60 s under the stop, shorter than the wait
        yes_bid(w, 0.55, 450 - 3 * k)
        assert signals(w) == []
    yes_bid(w, 0.80, 390)  # it recovers: the wait is cancelled
    assert signals(w) == []
    for k in range(0, 20):  # a second dip gets its own full wait
        yes_bid(w, 0.55, 387 - 3 * k)
        assert signals(w) == []


def test_integration_with_no_hold_the_stop_sells_at_once_as_before(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, hold=0)
    yes_bid(w, 0.55, 450)
    assert signals(w) != []


def test_unit_the_hold_changes_a_lanes_version_only_when_it_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for hold in (0, 0.5):
        (tmp_path / "strategies.toml").write_text(LANE.format(live="true", confirm=0.4, hold=hold, swing=0, floor=0))
        s = config.load()
        got = lanes.settings_of(lanes.lane_specs(s)["risk_only"], s)
        assert ("curve_risk_stop_hold" in got) is (hold > 0)


# ---- the swing hold, through the real watcher path (2026-11-14) ----
@pytest.fixture(autouse=True)
def _clean_mids():
    ticks_mod.mids._rows.clear()
    yield
    ticks_mod.mids._rows.clear()


def feed_history(step, from_left=900, to_left=300):
    """The market's own quotes, one a second, alternating up and down by `step` around 0.78: its typical minute is step * sqrt(60)."""
    mid = 0.78
    for k, left in enumerate(range(from_left, to_left - 1, -1)):
        ticks_mod.mids.note(TICKER, CLOSE - left, mid - 0.005, mid + 0.005)
        mid += step if k % 2 == 0 else -step


def dip(w, bid, left, seconds=30):
    """The bid sits at `bid` for `seconds`, one quote every 3 s, each also in the market's own quote history; returns the first sale signal or None."""
    for k in range(0, seconds, 3):
        ticks_mod.mids.note(TICKER, CLOSE - left + k, bid - 0.005, bid + 0.005)
        yes_bid(w, bid, left - k)
        out = signals(w)
        if out:
            return out[0]
    return None


def test_integration_a_dip_inside_the_markets_own_swings_is_not_sold_at_five_minutes_left(tmp_path, monkeypatch):
    feed_history(0.013)  # a typical minute of about 0.10: this market routinely moves 0.10 in a minute
    w = held(tmp_path, monkeypatch, live=True, swing=1.5)
    assert dip(w, 0.58, 300) is None  # 0.58 is a loss of 0.26 against a band of 1.5 * 0.10 * sqrt(5) = 0.34


def test_integration_the_same_dip_in_a_quiet_market_is_sold(tmp_path, monkeypatch):
    feed_history(0.0013)  # a typical minute of about 0.01
    w = held(tmp_path, monkeypatch, live=True, swing=1.5)
    sig = dip(w, 0.58, 300)
    assert sig is not None and sig.why.startswith("curve risk exit (max loss)")


def test_integration_the_same_swinging_market_sells_that_dip_when_the_close_is_near(tmp_path, monkeypatch):
    feed_history(0.013, to_left=40)
    w = held(tmp_path, monkeypatch, live=True, swing=1.5)
    sig = dip(w, 0.58, 40, seconds=9)  # the band is 1.5 * 0.10 * sqrt(40 / 60) = 0.12, under the loss of 0.26
    assert sig is not None


def test_integration_with_the_swing_off_the_swinging_markets_dip_is_sold_as_before(tmp_path, monkeypatch):
    feed_history(0.013)
    w = held(tmp_path, monkeypatch, live=True, swing=0)
    assert dip(w, 0.58, 300) is not None


def test_integration_with_no_quote_history_the_plain_stop_applies(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, swing=1.5)  # nothing in the market's own history: no reading of its swing, no hold
    assert dip(w, 0.58, 300) is not None


def test_integration_a_loss_sold_on_the_line_is_held_too(tmp_path, monkeypatch):
    feed_history(0.013)
    w = held(tmp_path, monkeypatch, live=True, swing=2.5)  # a band of 2.5 * 0.10 * sqrt(5) = 0.56: the loss of 0.37 at a bid of 0.45 is inside it
    assert dip(w, 0.45, 300, seconds=60) is None  # neither the max-loss stop nor the line sale (q under 0.5, after its wait) may sell it
    ticks_mod.mids._rows.clear()
    feed_history(0.013)
    w2 = held(tmp_path, monkeypatch, live=True, swing=0)
    assert dip(w2, 0.45, 300, seconds=60) is not None  # the same bet with the swing off sells


def test_integration_the_hold_is_logged_once_for_the_bet(tmp_path, monkeypatch, caplog):
    import logging

    feed_history(0.013)
    w = held(tmp_path, monkeypatch, live=True, swing=1.5)
    with caplog.at_level(logging.INFO, logger="moonshot"):
        dip(w, 0.58, 300, seconds=60)
    assert sum(1 for r in caplog.records if "holds a loss" in r.getMessage()) == 1


def test_unit_the_swing_changes_a_lanes_version_only_when_it_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for swing in (0, 1.5):
        (tmp_path / "strategies.toml").write_text(LANE.format(live="true", confirm=0.4, hold=0, swing=swing, floor=0))
        s = config.load()
        got = lanes.settings_of(lanes.lane_specs(s)["risk_only"], s)
        assert ("curve_risk_swing" in got) is (swing > 0)


# ---- the swing hold's floor (2026-11-14, user: "Floor at 10") ----
def test_integration_under_the_floor_the_swing_hold_lets_a_loss_sell(tmp_path, monkeypatch):
    feed_history(0.013)  # a typical minute of about 0.10
    w = held(tmp_path, monkeypatch, live=True, swing=4.0, floor=0.10)  # a band of 4 * 0.10 * sqrt(5) = 0.9: the loss at a bid of 0.08 (0.75) is inside it
    sig = dip(w, 0.08, 300)
    assert sig is not None and sig.why.startswith("curve risk exit (max loss)")


def test_integration_above_the_floor_the_same_band_still_holds(tmp_path, monkeypatch):
    feed_history(0.013)
    w = held(tmp_path, monkeypatch, live=True, swing=4.0, floor=0.10)
    assert dip(w, 0.30, 300) is None  # a loss of 0.52 at a bid of 0.30: inside the band and above the floor


def test_integration_with_no_floor_the_band_holds_a_bid_of_eight_cents(tmp_path, monkeypatch):
    feed_history(0.013)
    w = held(tmp_path, monkeypatch, live=True, swing=4.0, floor=0)
    assert dip(w, 0.08, 300) is None  # the ETH case: held all the way down


def test_integration_the_floor_is_a_chance_not_a_price_of_the_yes_side(tmp_path, monkeypatch):
    feed_history(0.013)
    w = held(tmp_path, monkeypatch, live=True, swing=4.0, floor=0.60)  # a floor above the bid's net chance of 0.567
    assert dip(w, 0.58, 300) is not None


def test_unit_the_floor_changes_a_lanes_version_only_when_set_with_the_swing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for floor in (0, 0.1):
        (tmp_path / "strategies.toml").write_text(LANE.format(live="true", confirm=0.4, hold=0, swing=1.5, floor=floor))
        s = config.load()
        got = lanes.settings_of(lanes.lane_specs(s)["risk_only"], s)
        assert ("curve_risk_swing_floor" in got) is (floor > 0)
