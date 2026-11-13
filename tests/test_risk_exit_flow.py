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
"""


class Client:
    """The market reads the watcher makes when it arms a position, and the sale it places."""

    def __init__(self, bid=0.56):
        self.bid = bid
        self.sold = []

    def market(self, ticker):
        return {"ticker": ticker, "floor_strike": 100.0, "close_time": "2030-01-01T00:15:00Z", "yes_bid_dollars": str(self.bid), "yes_ask_dollars": str(self.bid + 0.01)}

    def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.sold.append(price)
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "s"}


def held(tmp_path, monkeypatch, live, client=None, confirm=-1):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(live="true" if live else "false", confirm=confirm))
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
