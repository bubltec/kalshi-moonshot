"""A paper sale is recorded at the bid of the tick the step decided on, not at an older re-read of the market (2026-11-23).
KXNATGAS15M-26NOV230230 (wave_top, 07:26 UTC): the high-zone sale was decided on a tick at 0.86 and recorded at 0.65, the quote from before the jump, because the sale thread re-read the market (a REST GET that can be older than the tick) and kept the lower of the two. All 13 high-zone sales were recorded under the bid in their reason, 14c on average.
Integration: lane file -> refresh -> quote event -> sale thread -> the price in the decisions table. A live order still anchors to the lower of the tick and the read, on purpose."""
import time

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from tests.test_risk_exit_flow import CLOSE, TICKER, Client, held, yes_bid  # noqa: F401

WHY = "curve risk exit (profit take): a gain with the bid stopped"
LANE = "risk_only"


class Reads(Client):
    """The market read the sale thread makes, counted; its bid is the stale one."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reads = 0

    def market(self, ticker):
        self.reads += 1
        return super().market(ticker)


def exit_prices(tmp_path):
    db = Store(config.load().db_path).db
    return [r[0] for r in db.execute("SELECT exit_price FROM decisions WHERE ticker=? AND exit_price IS NOT NULL", (TICKER,)).fetchall()]


def test_integration_a_paper_sale_is_recorded_at_the_tick_bid_and_reads_no_market(tmp_path, monkeypatch):
    c = Reads(bid=0.86)  # the stale read: the bid from before the jump
    w = held(tmp_path, monkeypatch, live=False, client=c)
    yes_bid(w, 0.92, 700)  # the tick the step decided on
    setup = c.reads  # the read the lane's setup made when it armed the bet
    w._sell(TICKER, LANE, WHY)
    assert exit_prices(tmp_path) == [0.92]  # the old code recorded 0.86, the lower of the read and the tick
    assert c.reads == setup  # nothing is fetched on a paper sale that has the tick in memory


def test_integration_a_live_order_still_anchors_to_the_lower_of_the_tick_and_the_read(tmp_path, monkeypatch):
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    c = Reads(bid=0.86)
    w = held(tmp_path, monkeypatch, live=True, client=c)
    yes_bid(w, 0.92, 700)
    setup = c.reads
    w._sell(TICKER, LANE, WHY)
    assert c.reads == setup + 1 and c.sold, (c.reads, c.sold)
    assert c.sold[0][1] <= 0.86 + 1e-9  # the order is placed at or under the stale read less the slip (the live fill takes the book's best bid)


def test_integration_a_paper_sale_with_no_fresh_tick_falls_back_to_the_market_read(tmp_path, monkeypatch):
    c = Reads(bid=0.86)
    w = held(tmp_path, monkeypatch, live=False, client=c)
    yes_bid(w, 0.92, 700)
    w.targets[(TICKER, LANE)].last_ts = time.time() - 60.0  # the last tick is a minute old: not a price to sell at
    setup = c.reads
    w._sell(TICKER, LANE, WHY)
    assert c.reads == setup + 1 and exit_prices(tmp_path) == [0.86]


def test_integration_a_stale_read_cannot_sell_a_bid_that_has_gone_under_the_cost(tmp_path, monkeypatch):
    c = Reads(bid=0.92)  # the stale read still shows the old, high bid
    w = held(tmp_path, monkeypatch, live=False, client=c)
    yes_bid(w, 0.79, 700)  # the tick: under the 0.82 the bet cost with its fee
    w._sell(TICKER, LANE, WHY)
    assert exit_prices(tmp_path) == []  # a profit take never sells for a loss
    assert len(Store(config.load().db_path).open_bets()) == 1


# ---- the wave give-up is a sale at a loss by design (2026-11-23). _sell backs off a sale under the cost after both fees unless the reason is one of the exits that exist to get out; the wave salvage sells only for a small gain, the give-up must not be refused for being under the cost. ----
import pytest  # noqa: E402


@pytest.mark.parametrize("live", [False, True])
def test_integration_a_wave_give_up_sells_under_the_cost_and_the_wave_salvage_still_does_not(tmp_path, monkeypatch, live):
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": live)
    for why, sells in (("wave give-up: the bid fell and has not turned", True), ("wave salvage: the bid fell and has come back", False)):
        sub = tmp_path / ("a" if sells else "b")
        sub.mkdir()
        c = Reads(bid=0.50)  # well under the 0.81 the bet cost: a sale here is a loss after both fees
        w = held(sub, monkeypatch, live=live, client=c)
        yes_bid(w, 0.50, 700)
        w._sell(TICKER, LANE, why)
        open_bets = len(Store(config.load().db_path).open_bets())
        assert (open_bets == 0) is sells, (why, live, open_bets)
