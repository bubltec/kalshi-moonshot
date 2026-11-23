"""The risk exit end to end, through the real watcher path (2026-11-13). The first tests of the exit built a Target by hand and so never touched the step that failed live: a lane with only curve_risk_sell set was never given a watched position (takes_profit() was false), so nothing was armed and nothing could sell. These tests start from the lane file and the stored bet, as the bot does: Store row -> refresh() -> quote events -> sale signal."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_moonshot import config, curverisk, lanes, watcher
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
curve_risk_live={live}
curve_risk_spot_weight=0.0
curve_risk_history=false
curve_risk_confirm={confirm}
curve_take={take}
curve_take_slope_share={share}
curve_take_cushion={cushion}
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


def held(tmp_path, monkeypatch, live, client=None, reason=None, confirm=-1, hold=0, take='false', stretch=0, share=0.015, squeeze=0, max_loss=0.5, cushion='false', nudge_hour='false', long_scales='', extra=''):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(live="true" if live else "false", confirm=confirm, hold=hold, take=take, stretch=stretch, share=share, squeeze=squeeze, max_loss=max_loss, cushion=cushion) + (f"curve_risk_long_scales={long_scales}\n" if long_scales else "") + ("" if "sell_chain" in extra else "sell_chain=['never_ahead','under_even','profit_take','trail','long_hold']\n") + (extra + "\n" if extra else ""))
    s = config.load()
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (CLOSE - 450, TICKER, "bet", "trade", 0.81, "yes", "risk_only", 4.65, 3.77, "2030-01-01T00:15:00Z", reason))
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
    assert t is not None and t.sell_chain and t.risk_live and t.risk_spot_weight == 0.0 and t.close_ts > 0


def test_in_shadow_it_sells_nothing(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=False)
    for k, bid in enumerate(PATH):
        yes_bid(w, bid, 450 - 3 * k)
    assert signals(w) == []


def test_the_lanes_that_trade_live_are_all_watched(tmp_path, monkeypatch):
    """Guards the live config itself: every lane in LIVE_LANES must be one the watcher tracks (an exit module), or it has no exits at all (the 09:45 break). Reads the Mac's lane file and .env as text only, with settings from a scratch data dir."""
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
    unwatched = [n for n in names if n in specs and not specs[n].sell_chain]
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
    c = Client(bid=0.45, account={TICKER: 3.0})  # the user sold 1.65 of the 4.65 by hand
    w = held(tmp_path, monkeypatch, live=True, client=c, extra=UE)
    job = ue_signal(w)
    assert job is not None
    w._sell(*job)
    assert [n for n, _ in c.sold] == [3.0]  # one order, for the three contracts that are there
    assert len(Store(config.load().db_path).open_bets()) == 0


def test_integration_a_bet_sold_entirely_by_hand_sends_no_order_and_is_closed(tmp_path, monkeypatch):
    live(monkeypatch)
    c = Client(bid=0.45, account={})  # nothing of it left on the account
    w = held(tmp_path, monkeypatch, live=True, client=c, extra=UE)
    job = ue_signal(w)
    w._sell(*job)
    s = config.load()
    store = Store(s.db_path)
    assert c.sold == [] and len(store.open_bets()) == 0
    assert TICKER not in store.open_position_tickers(s.env)  # the lane may buy this market again
    row = store.db.execute("SELECT exit_reason FROM decisions WHERE ticker=?", (TICKER,)).fetchone()
    assert "closed by hand" in row["exit_reason"]


def test_integration_when_the_account_cannot_be_read_the_sale_goes_by_the_books(tmp_path, monkeypatch):
    live(monkeypatch)
    c = Client(bid=0.45, account=None)
    w = held(tmp_path, monkeypatch, live=True, client=c, extra=UE)
    w._sell(*ue_signal(w))
    assert [n for n, _ in c.sold] == [4.65]


def test_integration_a_sale_that_fills_nothing_backs_off_instead_of_firing_every_second(tmp_path, monkeypatch):
    live(monkeypatch)
    monkeypatch.setattr("kalshi_moonshot.runner.time.sleep", lambda x: None)
    c = Client(bid=0.30, fills=0.0, account=None)  # every order comes back unfilled
    w = held(tmp_path, monkeypatch, live=True, client=c, extra=UE)
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
def sold_after(w, bid, left, step=1, limit=200):
    """The bid sits at `bid` from `left` seconds to the close, one quote every `step` s; returns how many seconds passed before the sale, or None."""
    for k in range(0, limit, step):
        yes_bid(w, bid, left - k)
        if signals(w):
            return k
    return None


# The stop's wait (2026-11-14 evening, user: "The stops wait sounds incomplete"; "Is it missing a cap or something?"; "it also needs to be scaled on time left"):
# the market's recovery time (15 s here, the lane's default) x (1 - how deep the chance is under the line) x the share of the market still to run, never over the lane's share of the time left.
def test_integration_a_dip_that_recovers_inside_the_wait_does_not_sell_and_the_clock_restarts(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, hold=0.5)
    for k in range(0, 5):  # 5 s under the stop, shorter than the 7.5 s wait
        yes_bid(w, 0.55, 450 - k)
        assert signals(w) == []
    yes_bid(w, 0.80, 445)  # it recovers: the wait is cancelled
    assert signals(w) == []
    for k in range(0, 5):  # a second dip gets its own wait
        yes_bid(w, 0.55, 444 - k)
        assert signals(w) == []


# ---- helpers: a market's own quote history, and a bid that sits ----
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


# ---- the profit take, scaled by the bet and the clock (2026-11-14, user: "why not just use the bet?"; "earlier in the market is a better favor to sell for profit") ----
# the fixture bet: YES bought at 0.81, a cost of about 0.821 with the fee, so 0.179 of room
def climb(w, start, stop, left, step=0.01):
    """The bid climbs from start to stop one quote a second, then the sale signal (if any) is returned; the market's quotes also go into its history."""
    bid, k, sig = start, 0, None
    while bid <= stop + 1e-9:
        ticks_mod.mids.note(TICKER, CLOSE - left + k, round(bid, 4) - 0.005, round(bid, 4) + 0.005)
        yes_bid(w, round(bid, 4), left - k)
        out = signals(w)
        if out and sig is None:
            sig = out[0]
        bid += step
        k += 1
    return sig


def level(w, bid, left, seconds=12):
    for k in range(seconds):
        ticks_mod.mids.note(TICKER, CLOSE - left + 30 + k, bid - 0.005, bid + 0.005)
        yes_bid(w, bid, left - 30 - k)
        out = signals(w)
        if out:
            return out[0]
    return None


def test_integration_while_the_bid_is_still_climbing_nothing_is_sold(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true")
    assert climb(w, 0.82, 0.92, 700) is None  # 20% of the market gone: a gain of 0.07 is over the bar, but the bid has not stopped rising


def test_integration_when_the_climb_stops_the_gain_is_taken(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true")
    assert climb(w, 0.82, 0.92, 700) is None
    sig = level(w, 0.92, 700)  # the bid sits at 0.92 for 12 s: the slope is gone
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


def test_integration_the_same_gain_late_in_the_market_is_held(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true")
    climb(w, 0.82, 0.92, 100)
    assert level(w, 0.92, 100) is None  # 89% of the market gone: a gain of 0.07 is 40% of the room, under the bar of 89%


def test_integration_a_small_gain_early_is_taken_when_the_bid_levels_out(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true")
    climb(w, 0.82, 0.86, 850)
    sig = level(w, 0.86, 850)  # 6% of the market gone, a gain of 0.03 is 17% of the room, over the bar and the buy-back fee
    assert sig is not None


def test_integration_the_bid_turning_down_ends_the_slope(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true")
    assert climb(w, 0.82, 0.92, 700) is None
    yes_bid(w, 0.91, 688)
    assert signals(w) != []  # under the window's high: it has turned


def test_integration_in_a_swinging_market_a_one_tick_pause_does_not_end_the_slope(tmp_path, monkeypatch):
    feed_history(0.013, from_left=1300, to_left=800)  # a typical minute of about 0.10: 0.041 of noise over the 10 s look back
    w = held(tmp_path, monkeypatch, live=True, take="true")
    assert climb(w, 0.82, 0.92, 790) is None
    ticks_mod.mids.note(TICKER, CLOSE - 778, 0.905, 0.915)
    yes_bid(w, 0.91, 778)
    assert signals(w) == []  # one tick off the high, the bid still 0.08 up over the look back
    sig = level(w, 0.91, 777 + 30, seconds=14)  # then it sits: the rise ages out of the look back
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


def test_integration_in_a_swinging_market_a_fall_past_the_noise_ends_the_slope(tmp_path, monkeypatch):
    feed_history(0.013, from_left=1300, to_left=800)
    w = held(tmp_path, monkeypatch, live=True, take="true")
    assert climb(w, 0.82, 0.92, 790) is None
    ticks_mod.mids.note(TICKER, CLOSE - 778, 0.865, 0.875)
    yes_bid(w, 0.87, 778)  # 0.05 off the high, still well in profit
    assert signals(w) != []


def test_integration_the_take_is_off_by_default(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="false")
    climb(w, 0.82, 0.92, 700)
    assert level(w, 0.92, 700) is None


def test_integration_in_shadow_the_take_logs_and_sells_nothing(tmp_path, monkeypatch, caplog):
    import logging

    w = held(tmp_path, monkeypatch, live=False, take="true")
    with caplog.at_level(logging.INFO):
        climb(w, 0.82, 0.92, 700)
        assert level(w, 0.92, 700) is None
    assert any("would sell: curve risk exit (profit take)" in r.getMessage() for r in caplog.records)


def test_integration_the_take_sale_is_executed_as_an_order(tmp_path, monkeypatch):
    c = Client(bid=0.92)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w = held(tmp_path, monkeypatch, live=True, client=c, take="true")
    climb(w, 0.82, 0.92, 700)
    job = None
    for k in range(12):
        yes_bid(w, 0.92, 670 - k)
        if not w._jobs.empty():
            job = w._jobs.get_nowait()
            break
    assert job is not None
    w._sell(*job)
    assert len(c.sold) == 1
    assert len(Store(config.load().db_path).open_bets()) == 0


# ---- the stretch is for buys only (2026-11-14, user: "I only want the stretch for buying") ----
def test_integration_a_lane_with_the_stretch_set_does_not_sell_a_near_miss_of_the_takes_bar(tmp_path, monkeypatch):
    feed_history(0.013, from_left=1300, to_left=480)  # a swinging market: the removed stretch sale would have fired here
    w = held(tmp_path, monkeypatch, live=True, take="true", stretch=5)
    assert climb(w, 0.82, 0.91, 470) is None
    ticks_mod.mids.note(TICKER, CLOSE - 459, 0.895, 0.905)
    yes_bid(w, 0.90, 459)  # just under the bar, one tick off its high
    assert signals(w) == []


# ---- the slope's look back is a share of the time left (2026-11-14, user: "the 10s look back depends on how much time is left. It needs to be based on the time left") ----
def sit(w, bid, left, seconds):
    """The bid sits at `bid`, one quote a second from `left`; returns the first sale signal or None."""
    for k in range(seconds):
        ticks_mod.mids.note(TICKER, CLOSE - left + k, bid - 0.005, bid + 0.005)
        yes_bid(w, bid, left - k)
        out = signals(w)
        if out:
            return out[0]
    return None


def test_integration_early_in_the_market_a_pause_of_half_a_minute_does_not_end_the_climb(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", share=0.08)
    assert sit(w, 0.82, 760, 60) is None  # the bet sits just in profit, under the bar
    assert climb(w, 0.82, 0.96, 700) is None
    assert sit(w, 0.96, 685, 30) is None  # about 655 s left: the look back is 52 s and still reaches the start of the climb


def test_integration_early_in_the_market_the_gain_is_taken_once_the_pause_outlasts_the_look_back(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", share=0.08)
    assert sit(w, 0.82, 760, 60) is None
    assert climb(w, 0.82, 0.96, 700) is None
    sig = sit(w, 0.96, 685, 70)
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


def test_integration_late_in_the_market_the_same_pause_ends_the_climb(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", share=0.08)
    assert sit(w, 0.82, 360, 60) is None
    assert climb(w, 0.82, 0.96, 300) is None
    sig = sit(w, 0.96, 285, 30)  # about 260 s left: the look back is 21 s and the climb has aged out of it
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


# ---- the squeeze: the mirror of the buy's stretch, and as small (2026-11-14, user: "it's an adjustment not a feature. And it needs to consider time left and history ... They just nudge each decision ... a tiny factor. Like 1-2c. Maybe less") ----
# A loss just short of the max loss's depth counts as reaching it when it is short by less than the nudge: the lane's cap (two cents here) x the history (the bid is sliding) x the clock (the share of the market gone).
def slid(w, path, left):
    """The bid follows `path`, one quote a second from `left`; returns (bid, signal) of the first sale or None."""
    for k, bid in enumerate(path):
        ticks_mod.mids.note(TICKER, CLOSE - left + k, bid - 0.005, bid + 0.005)
        yes_bid(w, bid, left - k)
        out = signals(w)
        if out:
            return bid, out[0]
    return None


SIT = [0.78] * 12
# the fixture bet costs about 0.80 all-in; the max loss (0.5 x the chance) is reached at a net bid of about 0.571. A bid of 0.59 nets about 0.574, which is about 0.003 short of it; a bid of 0.60 about 0.017 short
TO_59 = SIT + [round(0.78 - 0.01 * k, 2) for k in range(1, 20)] + [0.59] * 6  # slides to 0.59 and stops: never reaches the max loss
TO_60 = SIT + [round(0.78 - 0.01 * k, 2) for k in range(1, 19)] + [0.60] * 6
NO_HOLDS = dict(hold=0, share=0.08)  # the bar alone, so the test reads the squeeze and nothing else
LIVE = dict(hold=0.5, share=0.08)  # the live lane's holds
CAP = 0.02  # two cents


def test_integration_without_the_squeeze_that_near_miss_is_not_sold(tmp_path, monkeypatch):
    feed_history(0.013, from_left=1300, to_left=121)
    w = held(tmp_path, monkeypatch, live=True, squeeze=0, **NO_HOLDS)
    assert slid(w, TO_59, 120) is None


def test_integration_a_near_miss_that_is_not_heading_down_is_not_squeezed(tmp_path, monkeypatch):
    """The same level reached and then bounced off: short of the bar by a fraction of a cent, but the recent history is not a slide."""
    feed_history(0.013, from_left=1300, to_left=121)
    w = held(tmp_path, monkeypatch, live=True, squeeze=CAP, **NO_HOLDS)
    assert slid(w, SIT + [0.59] + [0.70] * 14, 120) is None


def test_integration_a_miss_of_more_than_the_cap_is_never_squeezed(tmp_path, monkeypatch):
    feed_history(0.013, from_left=1300, to_left=121)
    w = held(tmp_path, monkeypatch, live=True, squeeze=CAP, **NO_HOLDS)
    assert slid(w, SIT + [round(0.78 - 0.01 * k, 2) for k in range(1, 17)] + [0.62] * 8, 120) is None  # slides to 0.62: about 4.5 cents short, over the two-cent cap whatever the clock says


def test_integration_the_squeeze_changes_nothing_else_the_live_holds_still_hold_with_nine_minutes_left(tmp_path, monkeypatch):
    """The two live sales of 16:20: a slide to the stop's depth two minutes after the buy with nine minutes left. Nothing sells."""
    feed_history(0.013, from_left=1300, to_left=671)
    w = held(tmp_path, monkeypatch, live=True, squeeze=CAP, **LIVE)
    assert slid(w, [0.78] * 60 + [round(0.78 - 0.01 * k, 2) for k in range(1, 26)], 670) is None


def test_integration_the_hour_view_still_holds_a_squeezed_sale(tmp_path, monkeypatch):
    feed_history(0.013, from_left=1300, to_left=121)
    w = held(tmp_path, monkeypatch, live=True, confirm=0.4, squeeze=CAP, **NO_HOLDS)
    monkeypatch.setattr(w, "_risk_long", lambda t, now, left: 0.9)
    assert slid(w, TO_59, 120) is None


# ---- the late cap on the profit take's bar (2026-11-14, user, on a SOL NO back at 0.96 with 30 s left that lost at the bell: "Yes we should have cashed that out") ----
def late_recovery(w):
    """With two minutes left the bet sits just in profit, climbs toward 0.96 and sits there (79% of its room made, 0.04 left to win); returns the first sale signal, on the way up or at the top."""
    assert sit(w, 0.82, 120, 20) is None
    return climb(w, 0.82, 0.96, 100) or sit(w, 0.96, 85, 12)


def test_integration_late_in_a_swinging_market_a_bet_at_ninety_six_is_held_while_flat_and_cashed_on_a_dip(tmp_path, monkeypatch):
    # 2026-11-14 (user: "a flat slope under 3 mins is perfectly fine, unless it starts to dip while profitable we should not sell"): this bet used to be cashed the moment it sat flat at 0.96 (SOL NO 15:30, "Yes we should have cashed that out"); now it is held while flat and cashed when the bid starts to fall
    seed_spot(monkeypatch, 0.008)  # a high cushion
    feed_history(0.013, from_left=1300, to_left=121)  # a typical minute of about 0.10: a swing of about 0.12 over the 85 s left
    w = held(tmp_path, monkeypatch, live=True, take="true", share=0.08)
    assert late_recovery(w) is None
    out = []
    for k in range(6):  # over 5 s the bid falls from 0.96 to 0.925: more than the noise (about 0.03), and still over the half-room bar
        left, bid = 70 - k, round(0.96 - 0.007 * k, 4)
        ticks_mod.mids.note(TICKER, CLOSE - left, bid - 0.005, bid + 0.005)
        yes_bid(w, bid, left)
        out += signals(w)
    assert out and out[0].why.startswith("curve risk exit (profit take)") and "half the room" in out[0].why


def test_integration_late_in_a_quiet_market_the_same_bet_rides_to_its_win(tmp_path, monkeypatch):
    feed_history(0.0013, from_left=1300, to_left=121)  # with the climb itself, a swing of under 0.02 over the time left against 0.04 left to win
    w = held(tmp_path, monkeypatch, live=True, take="true")
    assert late_recovery(w) is None


# ---- the profit take's cushion check (2026-11-14, user: "we just need a cushion check before we decide to sell (in the profit flow)") ----
def seed_spot(monkeypatch, over_strike, seconds_before_close=50.0):
    """The lane's spot index (BRTI for KXBTC15M, armed against the strike 100.0): a tick every 5 s from 2000 s before the close, `over_strike` (a share, +0.008 = 80 bp) over the strike on the YES side, alternating a hundredth either way."""
    fresh = ticks_mod.TickHistory()
    monkeypatch.setattr(ticks_mod, "history", fresh)
    start = CLOSE - 2000.0
    rows = [(start + 5.0 * k, 100.0 * (1.0 + over_strike) + (0.01 if k % 2 else -0.01)) for k in range(int((2000.0 - seconds_before_close) // 5.0))]
    fresh.warm("BRTI", rows, start)


def taken_after_climb(tmp_path, monkeypatch, cushion, over_strike):
    if over_strike is not None:
        seed_spot(monkeypatch, over_strike)
    w = held(tmp_path, monkeypatch, live=True, take="true", cushion=cushion)
    assert climb(w, 0.82, 0.92, 700) is None
    return level(w, 0.92, 700)


def test_unit_the_cushion_holds_when_its_chance_is_over_what_the_sale_pays():
    from kalshi_moonshot import curverisk

    assert curverisk.cushion_holds(0.95, 0.91) is True
    assert curverisk.cushion_holds(0.80, 0.91) is False
    assert curverisk.cushion_holds(0.91, 0.91) is False  # a tie sells: the take was already earned


def test_integration_the_lane_file_arms_the_cushion_check_on_the_watched_position(tmp_path, monkeypatch):
    seed_spot(monkeypatch, 0.008)
    w = held(tmp_path, monkeypatch, live=True, take="true", cushion="true")
    assert w.targets[(TICKER, "risk_only")].risk_take_cushion is True
    w2 = held(tmp_path / "off", monkeypatch, live=True, take="true") if (tmp_path / "off").mkdir() is None else None
    assert w2.targets[(TICKER, "risk_only")].risk_take_cushion is False


def test_integration_a_spot_well_over_the_strike_holds_the_take_that_would_have_sold(tmp_path, monkeypatch):
    assert taken_after_climb(tmp_path, monkeypatch, "true", 0.008) is None  # 80 bp over: the curve's chance is about 1, over the 0.91 the sale pays


def test_integration_the_same_bet_without_the_check_sells_as_before(tmp_path, monkeypatch):
    sig = taken_after_climb(tmp_path, monkeypatch, "false", 0.008)
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


def test_integration_a_spot_under_the_strike_still_sells_and_the_note_says_why(tmp_path, monkeypatch):
    sig = taken_after_climb(tmp_path, monkeypatch, "true", -0.004)
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)") and "[cushion: the spot's chance" in sig.why


def test_integration_with_no_spot_reading_the_take_goes_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(ticks_mod, "history", ticks_mod.TickHistory())  # no spot history at all
    w = held(tmp_path, monkeypatch, live=True, take="true", cushion="true")
    assert climb(w, 0.82, 0.92, 700) is None
    sig = level(w, 0.92, 700)
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)") and "[cushion: no spot reading" in sig.why


def test_integration_the_cushion_only_holds_the_profit_take_not_the_stop(tmp_path, monkeypatch):
    seed_spot(monkeypatch, 0.008)
    w = held(tmp_path, monkeypatch, live=True, take="true", cushion="true", hold=0)
    assert [s for s in (slide(w) or []) if "profit take" in s.why] == []  # a loss is no profit take, so the check never applies to it


def test_integration_in_a_swinging_market_a_steady_small_climb_is_still_a_slope(tmp_path, monkeypatch):
    feed_history(0.013, from_left=1300, to_left=800)  # a typical minute of about 0.10: more noise than a slow climb makes
    w = held(tmp_path, monkeypatch, live=True, take="true")
    assert climb(w, 0.84, 0.90, 700, step=0.002) is None  # the bid rises every second: 0.002 a second is under the noise, and still a slope (it sold on the noise bar before 2026-11-14)


# ---- a flat bid late in the market is held; only a dip sells (2026-11-14, user: "a flat slope under 3 mins is perfectly fine, unless it starts to dip while profitable we should not sell") ----
def test_integration_in_the_last_fifth_a_flat_bid_is_held_and_a_dip_sells(tmp_path, monkeypatch):
    seed_spot(monkeypatch, 0.008)  # the spot 80 bp over the strike: a high cushion
    feed_history(0.013, from_left=900, to_left=151)  # a typical minute of about 0.10
    w = held(tmp_path, monkeypatch, live=True, take="true", share=0.08)
    assert climb(w, 0.82, 0.99, 150) is None  # still rising
    assert level(w, 0.99, 150, seconds=12) is None  # flat at the top with 137 s left: held
    out = []
    for k in range(9):  # it starts to dip: over 8 s it falls from 0.99 to 0.945, more than the noise (about 0.036)
        left, bid = 108 - k, round(0.99 - 0.005625 * k, 4)
        ticks_mod.mids.note(TICKER, CLOSE - left, bid - 0.005, bid + 0.005)
        yes_bid(w, bid, left)
        out += signals(w)
    assert out and out[0].why.startswith("curve risk exit (profit take)")


def test_integration_before_the_last_fifth_a_flat_bid_still_sells(tmp_path, monkeypatch):
    feed_history(0.013, from_left=1300, to_left=800)
    w = held(tmp_path, monkeypatch, live=True, take="true")
    assert climb(w, 0.82, 0.92, 700) is None
    sig = level(w, 0.92, 700)  # 700 s left: outside the last fifth, so flat ends the hold as before
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


def test_integration_in_the_last_fifth_a_flat_bid_with_a_thin_cushion_sells(tmp_path, monkeypatch):
    # 2026-11-14 (user: "flat and low cushion is an acceptable sell"): the same flat bid, with the spot only 1 bp over the strike
    seed_spot(monkeypatch, 0.0001)
    feed_history(0.013, from_left=900, to_left=151)
    w = held(tmp_path, monkeypatch, live=True, take="true", share=0.08)
    assert climb(w, 0.82, 0.99, 150) is None
    sig = level(w, 0.99, 150, seconds=12)
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


def test_integration_in_the_last_fifth_a_flat_bid_with_no_spot_reading_sells_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(ticks_mod, "history", ticks_mod.TickHistory())
    feed_history(0.013, from_left=900, to_left=151)
    w = held(tmp_path, monkeypatch, live=True, take="true", share=0.08)
    assert climb(w, 0.82, 0.99, 150) is None
    assert level(w, 0.99, 150, seconds=12) is not None


# ---- no 0.95 line (2026-11-23, user: it is an older patch concept and a hard cap; it had held a flat bid at 0.95 and over with more than the last fifth left while the cushion backed the price; BTC 00:00 YES had sold at 0.955 with 419 s left on 2026-11-14). A flat bid sells like any other outside the last fifth; the helper below climbs to a top bid and holds it flat ----
def high_flat(tmp_path, monkeypatch, over_strike, top):
    seed_spot(monkeypatch, over_strike)
    feed_history(0.013, from_left=1300, to_left=401)  # a typical minute of about 0.10
    w = held(tmp_path, monkeypatch, live=True, take="true", share=0.08)
    assert climb(w, 0.82, top, 400, step=0.0025) is None  # still rising, slowly enough that the look back (32 s at 400 s left) has history behind it
    steps = int(round((top - 0.82) / 0.0025)) + 1  # the climb's quotes end this many seconds after 400 s left; the level starts right after them
    return level(w, top, 400 - steps + 30, seconds=40)


def test_integration_above_ninety_five_a_flat_bid_with_cushion_is_not_held_by_a_line_with_seven_minutes_left(tmp_path, monkeypatch):
    sig = high_flat(tmp_path, monkeypatch, 0.008, 0.96)  # 400 s left, nowhere near the last fifth: the old 0.95 line held this one; now it sells as the flat bid at 0.93 does
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


def test_integration_above_ninety_five_a_flat_bid_with_a_thin_cushion_sells(tmp_path, monkeypatch):
    sig = high_flat(tmp_path, monkeypatch, 0.0001, 0.96)  # the spot 1 bp over the strike
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


def test_integration_under_ninety_five_the_same_flat_bid_still_sells_with_seven_minutes_left(tmp_path, monkeypatch):
    sig = high_flat(tmp_path, monkeypatch, 0.008, 0.93)  # a high cushion, the market is not in its last fifth
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


# ---- a two cent allowance on the cushion check (2026-11-15, user: "Yes the .02"; ETH YES 07:23 sold at 0.88 with the spot's chance 0.858 against the 0.873 the sale paid, then 0.98) ----
def test_unit_the_cushion_allowance_holds_a_chance_within_two_cents_under_the_price():
    from kalshi_moonshot import curverisk

    nudge = curverisk.CUSHION_NUDGE
    assert nudge == 0.02
    assert curverisk.cushion_holds(0.858, 0.873, nudge) is True  # 0.015 short: held
    assert curverisk.cushion_holds(0.853, 0.873, nudge) is False  # exactly 0.02 short: sells (the chance must be over, not equal to, the price less the allowance)
    assert curverisk.cushion_holds(0.80, 0.873, nudge) is False
    assert curverisk.cushion_holds(0.858, 0.873) is False  # without the allowance it is the strict check


def cushion_take(tmp_path, monkeypatch, chance):
    """The lane file with the cushion check on; the spot's chance is set directly so the gap to the price is exact."""
    monkeypatch.setattr(watcher.ProfitWatcher, "_take_cushion", lambda self, t, now, left, net: chance(net))
    w = held(tmp_path, monkeypatch, live=True, take="true", cushion="true")  # the fixture's short look back, so the climb has history behind it
    assert climb(w, 0.82, 0.92, 700) is None
    return level(w, 0.92, 700)


def test_integration_a_chance_one_cent_under_the_price_holds_the_take(tmp_path, monkeypatch):
    assert cushion_take(tmp_path, monkeypatch, lambda net: net - 0.015) is None  # inside the allowance


def test_integration_a_chance_three_cents_under_the_price_still_sells_and_the_note_says_so(tmp_path, monkeypatch):
    sig = cushion_take(tmp_path, monkeypatch, lambda net: net - 0.03)
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)") and "is not within 0.02 of the" in sig.why


# ---- the long layer's hold fades over the last fifth (2026-11-15, ETH 09:00 YES: the stop wanted out from 104 s left, the long layer stayed at 0.44 to 0.61) ----
def slide_at(w, left0):
    for k, bid in enumerate(PATH):
        yes_bid(w, bid, left0 - 3 * k)
    return signals(w)


def with_hour_view(monkeypatch, chance):
    monkeypatch.setattr(watcher.ProfitWatcher, "_risk_long", lambda self, t, now, left: chance)


def test_integration_an_hour_view_of_half_still_holds_the_stop_with_seven_minutes_left(tmp_path, monkeypatch):
    with_hour_view(monkeypatch, 0.55)
    w = held(tmp_path, monkeypatch, live=True, confirm=0.4)
    assert slide_at(w, 450) == []  # 0.55 is over the lane's 0.4 and the market is not in its last fifth


def test_integration_a_strong_hour_view_still_holds_late(tmp_path, monkeypatch):
    with_hour_view(monkeypatch, 0.97)
    w = held(tmp_path, monkeypatch, live=True, confirm=0.4)
    assert slide_at(w, 60) == []  # 0.97 is over the faded bar of about 0.8


def held_rows(tmp_path):
    import json

    path = Path(tmp_path) / "risk_shadow.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines() if '"held"' in x] if path.exists() else []


def test_integration_a_hold_with_no_sale_wanted_writes_nothing(tmp_path, monkeypatch):
    seed_perp(monkeypatch, last_over_open=0.004)
    w = held(tmp_path, monkeypatch, live=True, confirm=0.4)
    for k in range(5):
        yes_bid(w, 0.80, 450 - 3 * k)  # the bet is fine: nothing is wanted, so nothing is held
    assert held_rows(tmp_path) == []


# ---- the hour's width factor scales the nudge (2026-11-15, user: "make the hourly factor market number affect the nudge amount") ----
def hour_fit(monkeypatch, k):
    from kalshi_moonshot import widthfit

    monkeypatch.setattr(widthfit, "latest", (k, CLOSE - 300.0, 60))


# ---- no fixed cap: the squeeze's allowed miss is the market's own typical minute times how strongly everything agrees (2026-11-15, user: "remove the cap, it needs to hold its own logically. Maybe the high nudge is a good indication") ----
def test_integration_without_a_cap_a_near_miss_that_is_not_sliding_still_waits(tmp_path, monkeypatch):
    feed_history(0.013, from_left=1300, to_left=121)
    w = held(tmp_path, monkeypatch, live=True, squeeze=0, **NO_HOLDS)
    assert slid(w, SIT + [0.59] + [0.70] * 14, 120) is None  # the history share is zero: nothing is allowed


def test_integration_without_a_cap_the_clock_still_shrinks_it_early_in_the_market(tmp_path, monkeypatch):
    (tmp_path / "early").mkdir()
    feed_history(0.013, from_left=1300, to_left=671)
    early = slid(held(tmp_path / "early", monkeypatch, live=True, squeeze=0, **NO_HOLDS), [0.78] * 60 + TO_60[len(SIT):] + [0.60] * 30, 670)
    assert early is None  # ten minutes left: the clock share is near zero, so is the nudge


# ---- the long layer read at several scales together (2026-11-15, user: "the long layer should look further, but we need the context of the current hour") ----
def test_integration_the_lanes_hour_view_scales_reach_the_watched_position(tmp_path, monkeypatch):
    seed_perp(monkeypatch, last_over_open=0.004)
    w = held(tmp_path, monkeypatch, live=True, confirm=0.4, long_scales="[15,30,60]")
    assert [tuple(x.risk_long_scales) for x in w.targets.values()] == [(15, 30, 60)]


def test_integration_the_hour_view_is_re_read_every_second_not_every_five(tmp_path, monkeypatch):
    """2026-11-15 night: the chance's cushion is the perp's newest tick, so it was up to 5 s old. A new tick that moves the cushion shows in the next reading a second or two later."""
    seed_perp(monkeypatch, last_over_open=0.004)
    w = held(tmp_path, monkeypatch, live=True, confirm=0.4, long_scales="[8,16,32]")
    t = next(iter(w.targets.values()))
    now = CLOSE - 110.0  # just after the last seeded tick, so a new tick can be appended
    first = w._risk_long(t, now, 110.0)
    assert first is not None
    assert w._risk_long(t, now + 0.5, 109.5) == first  # inside the second: the cached reading
    ticks_mod.history.note("KP_BTC", now + 1.8, 100.0 * (1.0 - 0.006))  # the perp drops 0.6% under the open
    second = w._risk_long(t, now + 2.0, 108.0)
    assert second is not None and second < first - 0.05  # seen two seconds later, not five


# ---- sure wins are not sold (2026-11-15 night, user: "if it stops these sure wins from selling, sure. I thought we had this fixed") ----
def test_unit_sure_win_holds_from_the_sure_bid_at_any_time_and_from_the_late_bid_in_the_last_fifth():
    assert curverisk.sure_win(0.96, 700.0, 0.95, -1.0) and curverisk.sure_win(0.95, 20.0, 0.95, -1.0)
    assert not curverisk.sure_win(0.94, 700.0, 0.95, -1.0)
    assert curverisk.sure_win(0.92, 150.0, 0.95, 0.90)  # the last fifth is 180 s: 0.92 is over the late bid
    assert not curverisk.sure_win(0.92, 400.0, 0.95, 0.90)  # not late: only the sure bid counts
    assert not curverisk.sure_win(0.99, 10.0, -1.0, -1.0)  # off: nothing is held


def test_integration_a_take_that_would_sell_at_a_bid_of_0_96_is_held_with_the_sure_hold_and_sold_without_it(tmp_path, monkeypatch):
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    w = held(tmp_path / "off", monkeypatch, live=True, take="true")
    assert climb(w, 0.82, 0.96, 700) is None
    sig = level(w, 0.96, 700)
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")  # as it was: the bid levels out and it sells
    ticks_mod.mids._rows.clear()
    w2 = held(tmp_path / "on", monkeypatch, live=True, take="true", extra="curve_take_sure=0.95")
    assert climb(w2, 0.82, 0.96, 700) is None
    assert level(w2, 0.96, 700) is None  # a sure win: held to settlement


def test_integration_a_bid_of_0_92_is_sold_early_and_held_late_with_the_late_hold(tmp_path, monkeypatch):
    (tmp_path / "early").mkdir()
    w = held(tmp_path / "early", monkeypatch, live=True, take="true", extra="curve_take_sure=0.95\ncurve_take_sure_late=0.90")
    climb(w, 0.82, 0.92, 700)
    sig = level(w, 0.92, 700)  # 700 s left: not the last fifth, 0.92 is under the sure bid: the take sells as before
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")


# ---- an early 0.95 is cashed, the sure hold starts after six minutes (2026-11-15 night, user: "if it ever hits 95 and we haven't passed 6 mins we should take and let the rebuy kick in") ----
def test_integration_a_bid_of_0_96_in_the_first_six_minutes_is_taken_even_while_it_is_still_climbing(tmp_path, monkeypatch):
    (tmp_path / "plain").mkdir()
    (tmp_path / "early").mkdir()
    w0 = held(tmp_path / "plain", monkeypatch, live=True, take="true")
    assert climb(w0, 0.82, 0.96, 700) is None  # without the option: still climbing, nothing sold
    ticks_mod.mids._rows.clear()
    w = held(tmp_path / "early", monkeypatch, live=True, take="true", extra="curve_take_early_bid=0.95\ncurve_take_sure=0.95")
    got = climb(w, 0.82, 0.96, 700)  # 200 s of the market gone: inside the first six minutes
    sigs = got if got is not None else None
    assert sigs is not None and "early take" in sigs.why


def test_integration_after_six_minutes_the_same_bid_is_a_sure_win_and_is_held(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", extra="curve_take_early_bid=0.95\ncurve_take_sure=0.95")
    climb(w, 0.82, 0.96, 450)  # 450 s gone: past the first six minutes
    assert level(w, 0.96, 450) is None


def test_integration_a_dip_after_a_sure_win_does_not_sell_it_the_hold_is_a_latch_not_a_line(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", extra="curve_take_sure=0.95")
    climb(w, 0.82, 0.96, 450)  # past the first six minutes: a sure win, latched (the helpers' events end at 409 s left)
    assert level(w, 0.96, 450) is None
    tgt = next(iter(w.targets.values()))
    assert tgt.risk_sure_latched is True
    assert sit(w, 0.92, 400, 12) is None  # the bid dips to 0.92, under the 0.95 line but over the take's bar: still the sure win, no sale
    assert tgt.risk_at > CLOSE - 395 and tgt.risk_sure_latched is True  # and the dip was really read (the check ran at 389 s left)
    assert sit(w, 0.89, 385, 12) is None  # and to 0.89: nothing sells
    assert tgt.risk_at > CLOSE - 380 and tgt.risk_sure_latched is True


def test_integration_without_the_sure_hold_the_same_bid_of_0_96_is_sold(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true")
    climb(w, 0.82, 0.96, 450)
    sig = level(w, 0.96, 450)
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")  # as it was; the hold is what keeps it


def test_integration_a_fall_far_past_the_markets_own_swing_releases_the_latch(tmp_path, monkeypatch):
    for k in range(700):  # ten minutes of quotes before the climb, so the market's typical minute can be read (without it the latch stays on by design)
        mid = 0.82 + (0.004 if k % 2 else -0.004)
        ticks_mod.mids.note(TICKER, CLOSE - 1200 + k, mid - 0.005, mid + 0.005)
    w = held(tmp_path, monkeypatch, live=True, take="true", extra="curve_take_sure=0.95")
    climb(w, 0.82, 0.96, 450)
    level(w, 0.96, 450)
    tgt = next(iter(w.targets.values()))
    assert tgt.risk_sure_latched is True
    sit(w, 0.30, 400, 6)  # a collapse far past what this market normally moves over the time left (the stop may sell it: the latch is read before)
    assert tgt.risk_sure_latched is False


def test_integration_a_held_sure_win_leaves_a_stored_row_so_its_effect_can_be_scored(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", extra="curve_take_sure=0.95")
    climb(w, 0.82, 0.96, 450)
    assert level(w, 0.96, 450) is None
    rows = held_rows(tmp_path)
    assert rows and any(r["held"].startswith("sure hold: ") for r in rows)


# ---- the profit take's trail (2026-11-16, user, on KXXRP15M-26NOV161745 NO bought at 0.60, up to 0.80 with five minutes left, short of the take's bar, sold at 0.25: "we should be tracking every market that we buy with a trail") ----
# the fixture bet again: YES at 0.81, about 0.821 with the fee, 0.179 of room. With 250 s left 72% of the market is gone, so the take's bar is about 0.13 and a climb to 0.90 (a gain of 0.073) is under it.
TRAIL = "curve risk exit (profit take): trail"


def tick(w, bid, left):
    """One quote, also in the market's own history; returns the sale signals it raised."""
    ticks_mod.mids.note(TICKER, CLOSE - left, bid - 0.005, bid + 0.005)
    yes_bid(w, bid, left)
    return signals(w)


def run_up(w):
    """A quiet market (a typical minute near 0.02), then the bid climbs to 0.90 with about four minutes left: ahead, and under the take's bar."""
    feed_history(0.002, from_left=900, to_left=262)
    return climb(w, 0.82, 0.90, 260)


def test_unit_the_trail_needs_a_gain_a_typical_minute_and_a_fall_of_that_size():
    assert curverisk.trail_hit(0.05, 0.01, 0.76, 0.80, 0.03, 1.0)  # ahead, 4c under the high, the typical minute is 3c
    assert not curverisk.trail_hit(0.05, 0.01, 0.78, 0.80, 0.03, 1.0)  # 2c under: inside the market's own swing
    assert curverisk.trail_hit(0.05, 0.01, 0.74, 0.80, 0.03, 2.0) and not curverisk.trail_hit(0.05, 0.01, 0.76, 0.80, 0.03, 2.0)  # the lane's units scale the fall
    assert not curverisk.trail_hit(0.005, 0.01, 0.76, 0.80, 0.03, 1.0)  # the gain does not pay for the buy-back
    assert not curverisk.trail_hit(-0.10, 0.01, 0.50, 0.80, 0.03, 1.0)  # under water: the loss exit's business
    assert not curverisk.trail_hit(0.05, 0.01, 0.76, 0.80, None, 1.0) and not curverisk.trail_hit(0.05, 0.01, 0.76, 0.80, 0.0, 1.0)  # no reading of the market's swing
    assert not curverisk.trail_hit(0.05, 0.01, 0.76, 0.80, 0.03, 0.0)  # off


def test_integration_a_bet_ahead_that_falls_a_typical_minute_off_its_high_is_sold(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", extra="curve_take_trail=1.0\ncurve_take_trail_live=true")
    assert run_up(w) is None  # under the bar, still rising: nothing to sell
    assert tick(w, 0.89, 250) == []  # one cent off the high: inside the market's own swing
    out = tick(w, 0.86, 249)  # four cents off the high, still 3c ahead after the fee
    assert out and out[0].why.startswith(TRAIL) and "0.90 high" in out[0].why


def test_integration_the_trail_is_off_by_default_and_the_same_fall_sells_nothing(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true")
    assert run_up(w) is None
    assert tick(w, 0.89, 250) == [] and tick(w, 0.86, 249) == []  # the take's bar is not met and nothing trails


def test_integration_in_shadow_the_trail_logs_once_records_and_sells_nothing(tmp_path, monkeypatch, caplog):
    import json
    import logging

    w = held(tmp_path, monkeypatch, live=True, take="true", extra="curve_take_trail=1.0")  # the risk exit is live, the trail is not
    with caplog.at_level(logging.INFO):
        run_up(w)
        assert tick(w, 0.86, 250) == [] and tick(w, 0.86, 249) == []
    assert sum(1 for r in caplog.records if f"would sell: {TRAIL}" in r.getMessage()) == 1
    rows = [json.loads(x) for x in (tmp_path / "risk_shadow.jsonl").read_text().splitlines()]
    assert [(x["kind"], x["live"], x["bid"]) for x in rows if x.get("kind")] == [("trail", False, 0.86)]


def test_integration_a_fall_that_takes_the_whole_gain_is_not_a_trail_sale(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", extra="curve_take_trail=1.0\ncurve_take_trail_live=true")
    run_up(w)
    assert tick(w, 0.80, 250) == []  # under what the bet cost: no profit to protect, and far from the max loss


def test_integration_with_no_reading_of_the_markets_swing_the_trail_does_not_fire(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", extra="curve_take_trail=1.0\ncurve_take_trail_live=true")
    for k, bid in enumerate((0.84, 0.87, 0.90)):  # quotes to the watcher only: the market's own history is empty
        yes_bid(w, bid, 260 - k)
    signals(w)
    yes_bid(w, 0.86, 250)
    assert signals(w) == []


def test_integration_a_sure_win_is_not_trailed_out(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, take="true", extra="curve_take_trail=1.0\ncurve_take_trail_live=true\ncurve_take_sure=0.95")
    feed_history(0.002, from_left=900, to_left=262)
    climb(w, 0.82, 0.96, 260)  # latched as a sure win at 0.95
    signals(w)
    assert tick(w, 0.93, 240) == []  # three cents off the high: a dip the latch holds through


def test_integration_the_trail_sale_is_executed_as_a_profit_sale_and_the_bet_closes(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    c = Client(bid=0.86)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w = held(tmp_path, monkeypatch, live=True, client=c, take="true", extra="curve_take_trail=1.0\ncurve_take_trail_live=true")
    run_up(w)
    ticks_mod.mids.note(TICKER, CLOSE - 250, 0.855, 0.865)
    yes_bid(w, 0.86, 250)
    job = w._jobs.get_nowait()
    assert job.why.startswith(TRAIL) and runner.is_profit_sale(job.why)  # sent once at the bid, never chased under the bet's cost
    w._sell(*job)
    assert len(c.sold) == 1
    assert len(Store(config.load().db_path).open_bets()) == 0


# ---- the slow drop check, shadow only (2026-11-16, user: "we do need to get another stop in, maybe like some kind of drop check: trend away from position for X amount of time by Y factor of the spot we bought at"; "Yes, shadow it") ----
# the fixture bet again: YES at 0.81 on a market whose spot index (BRTI) is armed against the strike 100.0. The bid stays at 0.70 throughout, over the line and inside the max loss, so the risk exit itself wants nothing.
DROP = "would sell (slow drop)"


def spot_path(monkeypatch, value_at):
    """A fresh tick history with the lane's spot index one reading a second from 1500 s before the open's end to the close, `value_at(left)` giving the level."""
    fresh = ticks_mod.TickHistory()
    monkeypatch.setattr(ticks_mod, "history", fresh)
    fresh.warm("BRTI", [(CLOSE - left, value_at(left)) for left in range(2400, 441, -1)], CLOSE - 2400.0)
    return fresh, value_at


def walk(w, path, bid=0.70, to_left=300):
    """The spot's tick and Kalshi's quote once a second from 440 s left; returns every sale signal raised."""
    fresh, value_at = path
    out = []
    for left in range(440, to_left - 1, -1):
        fresh.note("BRTI", CLOSE - left, value_at(left))
        yes_bid(w, bid, left)
        out += signals(w)
    return out


def quiet(left):
    return 0.01 * (1 if (left // 45) % 2 else -1)  # a small square wave, so the market has a typical minute to be read in


def sliding(left):
    """Just over the strike until 440 s left, then down two thousandths a second: across the strike ten seconds later and still falling."""
    return 100.02 + quiet(left) if left > 440 else 100.02 - 0.002 * (440 - left)


def dipping(left):
    """The same market with one 20-second dip under the strike that comes back."""
    return 99.95 if 400 >= left > 380 else 100.02 + quiet(left)


def far_over(left):
    """The same slide starting far over the strike: it trends away for as long, and never crosses."""
    return 100.60 + quiet(left) if left > 440 else 100.60 - 0.002 * (440 - left)


def drop_rows(tmp_path):
    import json

    f = tmp_path / "risk_shadow.jsonl"
    return [x for x in (json.loads(line) for line in f.read_text().splitlines()) if x.get("kind") == "drop"] if f.exists() else []


def test_integration_the_drop_check_is_off_by_default(tmp_path, monkeypatch, caplog):
    import logging

    path = spot_path(monkeypatch, sliding)
    w = held(tmp_path, monkeypatch, live=True, max_loss=0)
    with caplog.at_level(logging.INFO):
        assert walk(w, path) == []
    assert not any(DROP in r.getMessage() for r in caplog.records) and drop_rows(tmp_path) == []


# ---- the never-ahead sale (2026-11-17, user: "Ok so 'never profitable and 90s left.' Let's add that to the live lanes") ----
# the fixture bet: YES bought at 0.81 with 450 s left, about 0.821 with the fee. A bid of 0.78 is under cost and over every other line of the risk exit (max loss off), so nothing else sells it.
NEVER = "curve risk exit (never ahead)"


def quotes(w, bids_by_left):
    """Kalshi's quote at each (seconds left, bid) in turn; returns every sale signal raised."""
    out = []
    for left, bid in bids_by_left:
        yes_bid(w, bid, left)
        out += signals(w)
    return out


def flat(bid, from_left, to_left, step=5):
    return [(left, bid) for left in range(from_left, to_left - 1, -step)]


def test_unit_never_ahead_needs_the_mark_a_whole_view_a_buy_before_it_and_no_profit_ever():
    assert curverisk.never_ahead(False, 90.0, 90.0, 450.0, True)
    assert not curverisk.never_ahead(False, 91.0, 90.0, 450.0, True)  # before the mark
    assert not curverisk.never_ahead(True, 90.0, 90.0, 450.0, True)  # it was in profit once
    assert not curverisk.never_ahead(False, 90.0, 90.0, 110.0, True)  # bought under 30 s before the mark
    assert not curverisk.never_ahead(False, 90.0, 90.0, 450.0, False)  # not watched from the buy
    assert not curverisk.never_ahead(False, 90.0, 0.0, 450.0, True)  # off


def test_integration_a_bet_never_in_profit_is_sold_at_the_mark_and_not_before(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, max_loss=0, extra="curve_risk_never_ahead=90")
    assert quotes(w, flat(0.78, 445, 95)) == []  # under cost the whole way, and nothing sells before the mark
    out = quotes(w, [(90, 0.78)])
    assert out and out[0].why.startswith(NEVER) and "sells at 0.78" in out[0].why


def test_integration_a_bet_that_was_in_profit_once_is_kept(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, max_loss=0, extra="curve_risk_never_ahead=90")
    path = flat(0.78, 445, 305) + [(300, 0.86)] + flat(0.78, 295, 10)  # one reading over cost after the fee, then under it to the end
    assert quotes(w, path) == []
    assert w.targets[(TICKER, "risk_only")].risk_never_state == 1


def test_integration_the_rule_is_off_by_default(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, max_loss=0)
    assert quotes(w, flat(0.78, 445, 10)) == []


def test_integration_a_bet_bought_just_before_the_mark_is_kept(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, max_loss=0, extra="curve_risk_never_ahead=430")  # the fixture buy is at 450 s left: 20 s before this mark
    assert quotes(w, flat(0.78, 445, 10)) == []


def test_integration_a_bet_first_seen_long_after_its_buy_is_kept_its_past_is_unknown(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, max_loss=0, extra="curve_risk_never_ahead=90")
    assert quotes(w, flat(0.78, 300, 10)) == []  # the first reading comes 150 s after the buy, as after a restart


def test_integration_the_sale_signal_stays_until_the_bet_is_sold(tmp_path, monkeypatch):
    w = held(tmp_path, monkeypatch, live=True, max_loss=0, extra="curve_risk_never_ahead=90")
    quotes(w, flat(0.78, 445, 95))
    assert quotes(w, [(90, 0.78)])[0].why.startswith(NEVER)
    t = w.targets[(TICKER, "risk_only")]
    assert t.risk_never_state == 2  # the first order may not go through: the look is not repeated and the decision stands


def test_integration_in_shadow_it_logs_once_records_and_sells_nothing(tmp_path, monkeypatch, caplog):
    import json
    import logging

    w = held(tmp_path, monkeypatch, live=False, max_loss=0, extra="curve_risk_never_ahead=90")
    with caplog.at_level(logging.INFO):
        assert quotes(w, flat(0.78, 445, 10)) == []
    assert sum(1 for r in caplog.records if f"would sell: {NEVER}" in r.getMessage()) == 1
    rows = [json.loads(x) for x in (tmp_path / "risk_shadow.jsonl").read_text().splitlines()]
    assert [(x["kind"], x["live"], x["bid"], x["left"]) for x in rows if x.get("kind") == "never ahead"] == [("never ahead", False, 0.78, 90.0)]


def test_integration_the_never_ahead_sale_is_an_order_at_a_loss_and_the_bet_closes(tmp_path, monkeypatch):
    c = Client(bid=0.78)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w = held(tmp_path, monkeypatch, live=True, client=c, max_loss=0, extra="curve_risk_never_ahead=90")
    for left, bid in flat(0.78, 445, 90):
        yes_bid(w, bid, left)
    job = w._jobs.get_nowait()
    assert job.why.startswith(NEVER)
    w._sell(*job)
    assert len(c.sold) == 1 and len(Store(config.load().db_path).open_bets()) == 0


# ---- the under-even sale (2026-11-17, user, after platinum 09:30, Brent 10:00 and copper 10:15 were each bought near 0.90 and went to zero: "just had 3 zeros in the last hour. we need to fix this") ----
# the fixture bet: YES bought at 0.81 with 450 s left. The max loss is off and the line is the fixture's 0.5 with its own wait, so these tests keep the long layer out and read the new rule's own reason.
UNDER = "curve risk exit (under even)"
UE = "curve_risk_under_even=0.1\ncurve_risk_under_even_live=true"


def ue_signal(w, path=None):
    """The first sale signal of a favorite whose bid sits under even (the under-even step is the sale these account and order mechanics tests ride on: the loss step they used was removed 2026-11-19)."""
    for left, bid in path or (flat(0.78, 445, 305) + [(left, 0.45) for left in range(300, 200, -1)]):
        yes_bid(w, bid, left)
        if not w._jobs.empty():
            return w._jobs.get_nowait()
    return None


@pytest.fixture
def low_line(monkeypatch):
    """The lane's own line at 0.12, as the live commodity lanes run it: a bid of 0.45 is over it, so only the under-even sale can sell there."""
    import sys

    monkeypatch.setattr(sys.modules[__name__], "LANE", LANE.replace("curve_risk_sell=0.5", "curve_risk_sell=0.12"))


def test_unit_under_even_needs_a_favorite_a_bid_under_even_and_a_tenth_of_the_time_left():
    assert curverisk.under_even(0.45, 0.81, 30.0, 300.0, 0.1)  # 30 s under even with 300 s left
    assert not curverisk.under_even(0.45, 0.81, 29.0, 300.0, 0.1)  # a second short
    assert not curverisk.under_even(0.45, 0.81, 3.0, 30.0, 0.1)  # late, the wait is short but never under the 15 s floor
    assert curverisk.under_even(0.45, 0.81, 15.0, 30.0, 0.1)
    assert not curverisk.under_even(0.50, 0.81, 60.0, 300.0, 0.1)  # at even is not under it
    assert not curverisk.under_even(0.30, 0.40, 60.0, 300.0, 0.1)  # bought as the cheap side: under even is where it started
    assert not curverisk.under_even(0.45, 0.81, 60.0, 300.0, 0.0)  # off


def test_unit_under_even_needs_depth_in_the_markets_own_swing_over_the_time_left():
    # copper 14:00 on 2026-11-19: NO bought 0.87, bid 0.35 with 51 s left, 7 s under even, typical minute about 0.12; settled NO
    assert not curverisk.under_even(0.42, 0.87, 20.0, 51.0, 0.1, 0.12)  # 0.08 under even, a swing of 0.11 over 51 s: a coin flip
    assert not curverisk.under_even(0.30, 0.87, 7.0, 51.0, 0.1, 0.12)  # 7 s: under the floor
    assert curverisk.under_even(0.30, 0.87, 15.0, 51.0, 0.1, 0.12)  # 0.20 under even, more than that swing
    assert curverisk.under_even(0.42, 0.87, 15.0, 51.0, 0.1, None)  # no reading of the unit: the old rule
    assert curverisk.under_even(0.45, 0.81, 30.0, 300.0, 0.1, 0.01)  # a calm market: a small depth is enough


def test_integration_a_favorite_under_even_for_a_tenth_of_the_time_left_is_sold(tmp_path, monkeypatch, low_line):
    w = held(tmp_path, monkeypatch, live=True, max_loss=0, hold=0, extra=UE)
    out = quotes(w, flat(0.78, 445, 305) + [(left, 0.45) for left in range(300, 272, -1)])  # under even from 300 s left; at 273 s left it has been 27 s, short of a tenth (27.3 s)
    assert out == []
    out = quotes(w, [(272, 0.45)])  # 28 s under, a tenth of 272 s is 27.2 s
    assert out and out[0].why.startswith(UNDER) and "28s" in out[0].why


def test_integration_a_dip_under_even_that_comes_back_starts_the_count_again(tmp_path, monkeypatch, low_line):
    w = held(tmp_path, monkeypatch, live=True, max_loss=0, extra=UE)
    path = flat(0.78, 445, 305) + [(left, 0.45) for left in range(300, 285, -1)] + [(284, 0.55)] + [(left, 0.45) for left in range(283, 268, -1)]  # 15 s under, back over, 15 s under: never 27 s in one piece
    assert not any(j.why.startswith(UNDER) for j in quotes(w, path))


def test_integration_the_under_even_sale_is_off_by_default_and_shadow_without_its_live_switch(tmp_path, monkeypatch, caplog, low_line):
    import json
    import logging

    w = held(tmp_path, monkeypatch, live=False, max_loss=0)
    assert quotes(w, flat(0.78, 445, 305) + [(left, 0.45) for left in range(300, 200, -1)]) == []
    assert not (tmp_path / "risk_shadow.jsonl").exists() or "under even" not in (tmp_path / "risk_shadow.jsonl").read_text()
    ticks_mod.mids.clear()
    w = held(tmp_path, monkeypatch, live=True, max_loss=0, extra="curve_risk_under_even=0.1")  # the risk exit is live, this rule is not
    with caplog.at_level(logging.INFO):
        out = quotes(w, flat(0.78, 445, 305) + [(left, 0.45) for left in range(300, 240, -1)])
    assert not any(j.why.startswith(UNDER) for j in out)
    assert sum(1 for rec in caplog.records if f"would sell: {UNDER}" in rec.getMessage()) == 1
    rows = [json.loads(x) for x in (tmp_path / "risk_shadow.jsonl").read_text().splitlines()]
    assert [(x["kind"], x["live"]) for x in rows if x.get("kind") == "under even"] == [("under even", False)]


def test_integration_the_under_even_sale_is_an_order_at_a_loss_and_the_bet_closes(tmp_path, monkeypatch, low_line):
    c = Client(bid=0.45)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w = held(tmp_path, monkeypatch, live=True, client=c, max_loss=0, extra=UE)
    job = None
    for left, bid in flat(0.78, 445, 305) + [(left, 0.45) for left in range(300, 260, -1)]:
        yes_bid(w, bid, left)
        if not w._jobs.empty():
            job = w._jobs.get_nowait()
            break
    assert job is not None and job.why.startswith(UNDER)
    w._sell(*job)
    assert len(c.sold) == 1 and len(Store(config.load().db_path).open_bets()) == 0


def test_unit_the_under_even_sale_changes_a_lanes_version_only_when_it_is_live(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for extra, shown in (("", False), ("curve_risk_under_even=0.1\n", False), (UE + "\n", True)):
        (tmp_path / "strategies.toml").write_text(LANE.format(live="true", confirm=0.4, hold=0, take="true", stretch=0, share=0.015, squeeze=0, max_loss=0.5, cushion='false') + extra)
        s = config.load()
        assert ("curve_risk_under_even" in lanes.settings_of(lanes.lane_specs(s)["risk_only"], s)) is shown


# The sure hold, time based with no cut-off (2026-11-17, user, after cashing out KXNEAR15M-26NOV171500 by hand at about 0.95 with over four minutes left: "we want the sure win, but it should be time based"; "if there's 90s left and we are in the 90s, probably a good chance of winning"; "5 mins left? that is too much time"; "do not use hard limits").
def test_unit_the_sliding_bar_rises_with_the_time_left_and_has_no_step():
    bar = lambda left: curverisk.sure_bar(left, 0.90, slide=1 / 3)  # noqa: E731
    assert bar(0) == pytest.approx(0.90) and bar(60) == pytest.approx(0.92) and bar(90) == pytest.approx(0.93) and bar(180) == pytest.approx(0.96)
    assert bar(300) == pytest.approx(1.0) and bar(700) == pytest.approx(1.0)  # five minutes or more: nothing is sure
    steps = [bar(k + 1) - bar(k) for k in range(0, 299)]
    assert max(steps) - min(steps) < 1e-9 and min(steps) > 0  # a straight line: one second more left always asks the same little more
    assert curverisk.sure_bar(90, 0.90, slide=-1.0) is None and curverisk.sure_bar(90, -1.0, slide=1 / 3) is None  # off


def test_unit_a_sure_win_by_the_sliding_bar():
    sw = lambda bid, left: curverisk.sure_win(bid, left, -1.0, 0.90, slide=1 / 3)  # noqa: E731
    assert sw(0.94, 90) and not sw(0.92, 90)  # 90 s left: the bar is 0.93
    assert sw(0.91, 20) and not sw(0.89, 20)
    assert not sw(0.957, 282)  # the NEAR bet: the bar is 0.994 with 282 s left
    assert not sw(0.999, 300) and not sw(0.999, 700)  # no bid is a sure win with five minutes or more left
    assert curverisk.sure_win(0.92, 150.0, -1.0, 0.90) and not curverisk.sure_win(0.92, 400.0, -1.0, 0.90)  # a lane without the slide: as it always was


def test_integration_in_the_90s_with_minutes_left_it_sells_and_near_the_close_it_holds(tmp_path, monkeypatch):
    extra = "curve_take_sure=-1\ncurve_take_sure_late=0.90\ncurve_take_sure_slide=0.333"
    (tmp_path / "early").mkdir()
    (tmp_path / "late").mkdir()
    w = held(tmp_path / "early", monkeypatch, live=True, take="true", extra=extra)
    assert w.targets[(TICKER, "risk_only")].risk_take_sure_slide == 0.333
    assert climb(w, 0.82, 0.95, 400) is None
    sig = level(w, 0.95, 400)
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")  # over six minutes left: the gain is taken
    ticks_mod.mids._rows.clear()
    w2 = held(tmp_path / "late", monkeypatch, live=True, take="true", extra=extra)
    assert climb(w2, 0.82, 0.95, 85) is None
    assert level(w2, 0.95, 85) is None  # 85 s left, the bar is under 0.93: held to settlement

