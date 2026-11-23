"""The gain-side wave sale rests at the ask instead of taking the bid (restsell.py, lane field wave_rest_share; 2026-11-23, user: "make sure our exit is fee-less if possible").

Unit: the state machine (rest on a gain-side sale; follow the ask both ways; fill when the bid reaches the price; the window ends and the bid is taken; a price that nets under the cost is never rested; another exit withdraws it; share 0 is off).
Integration: lane file -> refresh -> quote ticks -> the exit chain (wave_take's staircase) -> the sale thread -> the decisions table, on paper; and a live lane with a fake client: place, amend in place, the fallback, cancel and take, a fill on the socket."""
import time

import pytest

from kalshi_moonshot import bankroll, config, lanes, manualrest, memguard, restsell, wavetop
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract
from tests.test_risk_exit_flow import CLOSE, TICKER, Client, held

LANE = "risk_only"
TAKE = "wave take: the bid 0.84 fell under its last swing low 0.85 (swings of 0.026, cushion factor 1.00, 388 s left): the climb is done, sold at a gain of 1.0c a contract [sell chain lane: wave_take | sold at wave_take]"
GIVE_UP = "wave give-up: the bid fell to 0.60 and has not turned"


# ---- unit: the state machine ----
def rest(share=0.2, reason=TAKE, ask=0.85, entry=0.81, now=1000.0, close=1400.0, live=False):
    return restsell.start(share, reason, ask, entry, now, close, 8.0, live=live)


def test_unit_a_gain_side_sale_rests_at_the_ask_for_a_share_of_the_time_left():
    r = rest()
    assert r is not None and r.price == 0.85 and r.first == 0.85
    assert r.window == pytest.approx(0.2 * 400.0) and r.deadline == pytest.approx(1080.0)  # never fixed seconds: the share of the time left
    assert rest(now=1300.0).window == pytest.approx(0.2 * 100.0)  # later in the market, a shorter window
    assert rest(share=0.99, now=1300.0).deadline == pytest.approx(1400.0 - 8.0)  # never into the last seconds, where an order lands after the close
    for reason in ("wave salvage: the bid fell and came back", "value sale: the bid pays the hold value"):
        assert rest(reason=reason) is not None


def test_unit_off_by_default_and_the_give_up_and_other_sales_never_rest():
    assert lanes.LaneSpec.__dataclass_fields__["wave_rest_share"].default == 0.0
    assert rest(share=0.0) is None  # the switch is off
    assert rest(reason=GIVE_UP) is None  # the give-up stays an immediate taker sale
    assert rest(reason="curve risk exit (profit take): a gain") is None  # not a wave sale
    assert rest(ask=1.0) is None and rest(ask=0.0) is None  # no ask


def test_unit_a_price_that_nets_under_the_cost_after_fees_is_never_rested():
    basis = 0.81 + fee_per_contract(0.81)
    assert not restsell.is_gain(0.82, 0.81) and 0.82 - fee_per_contract(0.82) < basis
    assert rest(ask=0.82) is None  # a small-loss staircase sale whose ask is no gain stays a taker sale
    r = rest(ask=0.85)
    what, why = restsell.tick(r, 0.80, 0.82, 0.81, 1010.0)  # the ask falls to a price that is no longer a gain
    assert what == "ended" and why.startswith("wave take") and "no longer a gain" in why and "the bid 0.80 taken" in why


def test_unit_the_order_follows_the_ask_both_ways():
    r = rest()
    assert restsell.tick(r, 0.84, 0.85, 0.81, 1001.0) == ("", "")  # the same ask: nothing
    what, note = restsell.tick(r, 0.84, 0.87, 0.81, 1002.0)
    assert what == "moved" and r.price == 0.87 and "0.850 to 0.870" in note
    what, note = restsell.tick(r, 0.84, 0.86, 0.81, 1003.0)
    assert what == "moved" and r.price == 0.86 and r.moves == 2 and "0.870 to 0.860" in note


def test_unit_a_paper_rest_fills_when_the_bid_reaches_its_price_and_is_booked_there_with_no_fee():
    r = rest()
    restsell.tick(r, 0.84, 0.87, 0.81, 1002.0)  # moved up to 0.87
    assert restsell.tick(r, 0.86, 0.88, 0.81, 1004.0)[0] == "moved"  # the bid under the price: no fill (the ask moved again)
    what, why = restsell.tick(r, 0.88, 0.89, 0.81, 1012.0)  # the bid came up to the order's 0.88
    assert what == "filled" and why.startswith(manualrest.REST_PREFIX)
    assert manualrest.rest_fill_price(why) == pytest.approx(0.88)  # the paper exit books this price
    assert manualrest.sale_fee(0.88, why) == 0.0 and manualrest.sale_fee(0.88, TAKE) == pytest.approx(fee_per_contract(0.88))
    assert "wave take: the bid 0.84 fell under its last swing low" in why and "first 0.850" in why and "12s of its 80s window" in why  # the step's own reading stays, and how long it rested
    assert restsell.tick(r, 0.90, 0.91, 0.81, 1013.0) == ("", "")  # handed off once


def test_unit_the_window_ends_and_the_bid_is_taken():
    r = rest()
    assert restsell.tick(r, 0.83, 0.85, 0.81, 1079.0) == ("", "")
    what, why = restsell.tick(r, 0.83, 0.85, 0.81, 1080.0)
    assert what == "ended" and why.startswith("wave take: the bid 0.84") and "the window ended: the bid 0.83 taken" in why and "80s of its 80s window" in why


def test_unit_a_live_rest_is_filled_by_the_accounts_fills_not_by_the_tick():
    r = rest(live=True)
    assert restsell.tick(r, 0.90, 0.91, 0.81, 1010.0)[0] == "moved"  # the tick only moves it


def test_unit_another_exit_withdraws_the_rest_and_a_gain_side_sale_keeps_it():
    r = rest()
    assert restsell.keeps(r, TAKE, 0.81) and restsell.keeps(r, "value sale: the bid pays", 0.81)
    assert not restsell.keeps(r, GIVE_UP, 0.81) and not restsell.keeps(r, "curve risk exit: a loss", 0.81)
    why = restsell.stopped_reason(r, 1010.0, GIVE_UP)
    assert why.startswith("wave give-up") and "was withdrawn" in why


def test_unit_a_long_reason_keeps_its_start_and_its_chain_tag():
    long = "wave take: " + "x" * 2000 + " [sell chain lane: wave_take | sold at wave_take]"
    r = rest(reason=long)
    filled = restsell.tick(r, 0.86, 0.87, 0.81, 1001.0)[1]
    assert len(filled) <= restsell.KEEP and filled.startswith(manualrest.REST_PREFIX) and filled.endswith("sold at wave_take]")
    r = rest(reason=long)
    ended = restsell.tick(r, 0.83, 0.85, 0.81, 1100.0)[1]
    assert len(ended) <= restsell.KEEP and ended.startswith("wave take: xxx") and "sold at wave_take]" in ended and ended.endswith("taken]")


def test_unit_the_paper_pnl_counts_no_exit_fee_on_a_resting_fill(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    store = Store(s.db_path)
    for k, why in enumerate((restsell.filled_reason(rest(), 1010.0), TAKE)):
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, exit_price, exit_ts, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (1000.0 + k, f"T{k}", "bet", "observe", 0.81, "yes", f"l{k}", 10.0, 8.2, 0.85, 1010.0, why))
    store.db.commit()
    pnl = bankroll.paper_pnl(store)
    assert pnl["l0"]["returned"] == pytest.approx(10 * 0.85)  # the resting fill: no fee
    assert pnl["l1"]["returned"] == pytest.approx(10 * (0.85 - fee_per_contract(0.85)))  # a taker sale pays it


# ---- integration: lane file -> refresh -> ticks -> the exit chain -> the sale thread ----
@pytest.fixture
def mem():
    ticks_mod.history.clear()
    ticks_mod.mids.clear()
    wavetop._memo.clear()
    yield
    ticks_mod.history.clear()
    ticks_mod.mids.clear()
    wavetop._memo.clear()


def wave(tmp_path, monkeypatch, share=0.2, client=None):
    """The fixture bet (YES bought at 0.81, 4.65 contracts) on a lane whose exit is the wave staircase, with the lane's resting share; the bid's typical minute held at 0.04 so the staircase's swing is about 2.6c with 388 s left."""
    w = held(tmp_path, monkeypatch, live=True, client=client, extra=f"sell_chain=['wave_take']\nwave_rest_share={share}\nwave_whipsaw_range=0.0\nwave_arm_swings=0.0\nwave_cushion=false")
    t = w.targets[(TICKER, LANE)]
    assert t.wave_rest_share == share  # the lane file reached the target through the refresh
    monkeypatch.setattr(w, "_risk_unit", lambda tt, now: 0.04)
    return w, t


def tick(w, bid, left, ask=None):
    with memguard.decision("sell"):  # no database read and no Kalshi read on the tick
        w.on_event({"kind": "quote", "ticker": TICKER, "bid": bid, "ask": round(bid + 0.01, 4) if ask is None else ask, "ts": CLOSE - left})


def jobs(w):
    out = []
    while not w._jobs.empty():
        out.append(w._jobs.get_nowait())
    return out


STAIR = [0.84, 0.88, 0.85, 0.88, 0.92, 0.89]  # a climb, a pullback, a bounce (the low 0.85 confirmed), a higher high, a pullback


def to_lower_low(w, start=400):
    left = start
    for bid in STAIR:
        tick(w, bid, left)
        left -= 2
    tick(w, 0.84, left)  # under the last swing low 0.85, a gain after both fees: the staircase sells
    return left


def booked():
    db = Store(config.load().db_path).db
    return db.execute("SELECT exit_price, exit_reason FROM decisions WHERE ticker=? AND exit_price IS NOT NULL", (TICKER,)).fetchall()


def test_integration_share_zero_sells_at_the_bid_at_once_as_before(tmp_path, monkeypatch, mem):
    w, t = wave(tmp_path, monkeypatch, share=0.0)
    to_lower_low(w)
    got = jobs(w)
    assert len(got) == 1 and got[0].why.startswith("wave take") and "last swing low 0.85" in got[0].why
    assert t.rest is None


def test_integration_a_staircase_gain_rests_at_the_ask_and_fills_when_the_bid_comes_up(tmp_path, monkeypatch, mem):
    w, t = wave(tmp_path, monkeypatch)
    left = to_lower_low(w)
    assert jobs(w) == [] and t.rest is not None and t.rest.price == pytest.approx(0.85) and not t.rest.live  # rests at the ask, sells nothing at the bid
    assert t.rest.window == pytest.approx(0.2 * left)
    tick(w, 0.83, left - 2, ask=0.86)  # the ask moves up: the order follows
    assert t.rest.price == pytest.approx(0.86) and jobs(w) == []
    tick(w, 0.84, left - 4, ask=0.85)  # and back down
    assert t.rest.price == pytest.approx(0.85) and jobs(w) == []
    tick(w, 0.85, left - 6, ask=0.86)  # the bid reaches the order's 0.85: filled
    got = jobs(w)
    assert len(got) == 1 and got[0].why.startswith(manualrest.REST_PREFIX)
    w._sell(*got[0])  # the sale thread books it
    (price, why), = booked()
    assert price == pytest.approx(0.85) and why.startswith(manualrest.REST_PREFIX + " filled at 0.8500") and "wave take: the bid 0.84 fell under its last swing low 0.85" in why
    assert manualrest.sale_fee(price, why) == 0.0  # fee-free wherever the exit fee is counted
    pnl = bankroll.paper_pnl(Store(config.load().db_path), live=True)[LANE]  # the fixture bet's row is mode 'trade'; it is a paper sale all the same
    assert pnl["returned"] == pytest.approx(4.65 * 0.85)
    assert t.rest is None


def test_integration_the_bid_never_reaches_it_and_the_bid_is_taken_at_the_windows_end(tmp_path, monkeypatch, mem):
    w, t = wave(tmp_path, monkeypatch)
    left = to_lower_low(w)
    deadline_left = CLOSE - t.rest.deadline
    k = left - 2
    while k > deadline_left:
        tick(w, 0.84, k, ask=0.86)
        assert jobs(w) == []
        k -= 5
    tick(w, 0.845, deadline_left - 1, ask=0.86)
    got = jobs(w)
    assert len(got) == 1 and got[0].why.startswith("wave take: the bid 0.84") and "the window ended: the bid 0.84 taken" in got[0].why
    w._sell(*got[0])
    (price, why), = booked()
    assert price == pytest.approx(0.845) and why.startswith("wave take") and not manualrest.is_rest_fill(why)  # a taker sale at the bid, its fee counted as before


def test_integration_another_exit_withdraws_the_rest_and_takes_the_bid(tmp_path, monkeypatch, mem):
    w, t = wave(tmp_path, monkeypatch)
    left = to_lower_low(w)
    assert t.rest is not None
    calls = []
    monkeypatch.setattr(w, "_risk_quote_hit", lambda tt, bid, now: calls.append(bid) or GIVE_UP)  # the walk now decides the give-up
    tick(w, 0.83, left - 2, ask=0.86)
    got = jobs(w)
    assert calls and len(got) == 1 and got[0].why.startswith("wave give-up") and "was withdrawn" in got[0].why
    w._sell(*got[0])
    assert t.rest is None


def test_integration_the_tick_path_stays_in_memory_and_under_the_budget(tmp_path, monkeypatch, mem):
    w, t = wave(tmp_path, monkeypatch, share=0.5)
    left = to_lower_low(w)
    assert t.rest is not None
    n, began = 0, time.perf_counter()
    for k in range(200):  # a resting sale followed tick by tick (the ask flutters, the bid stays under)
        tick(w, 0.84, left - 1 - k * 0.5, ask=0.86 if k % 2 else 0.85)
        n += 1
    took = (time.perf_counter() - began) / n
    print(f"one sell tick with a resting sale: {took * 1000:.3f} ms")
    assert t.rest is not None and t.rest.moves > 100 and jobs(w) == []
    assert took < 0.060


# ---- live: a fake exchange ----
class Exchange(Client):
    def __init__(self, refuse_amend=False, **k):
        super().__init__(**k)
        self.calls = []
        self.refuse_amend = refuse_amend
        self.n = 0

    def sell_resting(self, ticker, side, count, price, seconds, client_order_id=None, post_only=True):
        self.n += 1
        self.calls.append(("place", side, count, round(price, 4), round(seconds)))
        return {"order_id": f"r{self.n}"}

    def amend_resting(self, order_id, ticker, side, action, price, count, seconds=None):
        from kalshi_moonshot.kalshi import KalshiError

        self.calls.append(("amend", order_id, side, action, round(price, 4), count))
        if self.refuse_amend:
            raise KalshiError("amend not supported")
        return {"order_id": order_id}

    def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        return {}

    def portfolio_rows(self, path, key, since, pages=1):
        return []

    def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.calls.append(("take", count, round(price, 2)))
        return super().sell_yes_ioc(ticker, count, price, client_order_id)


def live_wave(tmp_path, monkeypatch, **k):
    from tests.test_risk_exit_flow import live

    live(monkeypatch)
    ex = Exchange(bid=0.84, **k)
    w, t = wave(tmp_path, monkeypatch, client=ex)
    assert t.rest_live
    return w, t, ex


def run_jobs(w):
    """What the sell thread does with the queued work, in order (the thread is not started in a test)."""
    from kalshi_moonshot.watcher import RestOrder

    for job in jobs(w):
        if isinstance(job, RestOrder):
            w._rest_order(job)
        else:
            w._sell(*job)


def test_integration_live_places_amends_in_place_then_cancels_and_takes_the_bid_at_the_windows_end(tmp_path, monkeypatch, mem):
    w, t, ex = live_wave(tmp_path, monkeypatch)
    left = to_lower_low(w)
    assert ex.calls == []  # the tick places nothing itself
    run_jobs(w)
    assert ex.calls[0][:4] == ("place", "yes", 4.65, 0.85) and t.rest.order_id == "r1"
    tick(w, 0.84, left - 2, ask=0.87)
    run_jobs(w)
    assert ex.calls[-1] == ("amend", "r1", "yes", "sell", 0.87, 4.65) and t.rest.placed == pytest.approx(0.87)  # moved in place, same order
    tick(w, 0.84, CLOSE - t.rest.deadline - 1, ask=0.87)  # the window is over
    run_jobs(w)
    kinds = [c[0] for c in ex.calls]
    assert kinds == ["place", "amend", "cancel", "take"] and ex.calls[2] == ("cancel", "r1")  # cancelled before the taker sale of the same contracts
    (_price, why), = booked()
    assert why.startswith("wave take") and "the window ended" in why


def test_integration_live_an_amend_refused_falls_back_to_cancel_and_place_keeping_the_deadline(tmp_path, monkeypatch, mem):
    w, t, ex = live_wave(tmp_path, monkeypatch, refuse_amend=True)
    left = to_lower_low(w)
    run_jobs(w)
    deadline = t.rest.deadline
    tick(w, 0.84, left - 2, ask=0.86)
    run_jobs(w)
    assert [c[0] for c in ex.calls] == ["place", "amend", "cancel", "place"] and ex.calls[-1][:4] == ("place", "yes", 4.65, 0.86)
    assert t.rest.order_id == "r2" and t.rest.deadline == deadline and t.rest.ids == ["r1", "r2"]


def test_integration_live_a_fill_on_the_socket_books_the_sale_at_the_orders_price(tmp_path, monkeypatch, mem):
    w, t, ex = live_wave(tmp_path, monkeypatch)
    to_lower_low(w)
    run_jobs(w)
    w.on_event({"kind": "fill", "ticker": TICKER, "side": "yes", "action": "sell", "count": 4.65, "price": 0.85, "order_id": "r1", "ts": time.time()})
    run_jobs(w)
    (price, why), = booked()
    assert price == pytest.approx(0.85) and why.startswith(manualrest.REST_PREFIX) and t.rest is None
    db = Store(config.load().db_path).db
    sale = db.execute("SELECT fill_count, cost_usd, fee_usd, order_id FROM orders WHERE side='sell'").fetchall()
    assert [tuple(x) for x in sale] == [(4.65, pytest.approx(-4.65 * 0.85), 0.0, "r1")]
    assert "take" not in [c[0] for c in ex.calls]


def test_integration_live_a_give_up_cancels_the_resting_order_before_its_taker_sale(tmp_path, monkeypatch, mem):
    w, _t, ex = live_wave(tmp_path, monkeypatch)
    left = to_lower_low(w)
    run_jobs(w)
    monkeypatch.setattr(w, "_risk_quote_hit", lambda tt, bid, now: GIVE_UP)
    tick(w, 0.83, left - 2, ask=0.85)
    run_jobs(w)
    assert [c[0] for c in ex.calls] == ["place", "cancel", "take"]


def test_unit_a_lane_file_names_the_share_and_a_bad_one_is_refused(tmp_path, monkeypatch):
    from tests.test_wave_top import TOML

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(TOML)
    assert lanes.lane_specs(config.load())["wt"].wave_rest_share == 0.0
    (tmp_path / "strategies.toml").write_text(TOML.replace("wave_top_trend_hours = 1.0", "wave_top_trend_hours = 1.0\nwave_rest_share = 0.25"))
    assert lanes.lane_specs(config.load())["wt"].wave_rest_share == 0.25
    (tmp_path / "strategies.toml").write_text(TOML.replace("wave_top_trend_hours = 1.0", "wave_top_trend_hours = 1.0\nwave_rest_share = 1.5"))
    with pytest.raises(ValueError, match="wave_rest_share"):
        lanes.lane_specs(config.load())
