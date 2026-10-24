import time
from dataclasses import replace

import pytest

from kalshi_moonshot import config, kalshiws, watcher
from kalshi_moonshot.store import Store

FILL = {"type": "fill", "msg": {
    "trade_id": "t1", "order_id": "o1", "market_ticker": "KXBTC15M-26OCT241000-T80000", "is_taker": True, "side": "yes",
    "purchased_side": "yes", "action": "buy", "yes_price_dollars": "0.4400", "count_fp": "4.00", "ts_ms": 1,
}}


def test_a_fill_message_becomes_a_flat_event():
    e = kalshiws.parse_message(FILL, received=100.0)
    assert e == {"kind": "fill", "ts": 100.0, "ticker": "KXBTC15M-26OCT241000-T80000", "side": "yes", "action": "buy",
                 "count": 4.0, "price": 0.44, "taker": True}


def test_a_no_purchase_reports_the_side_bought():
    msg = {"type": "fill", "msg": {**FILL["msg"], "side": "yes", "purchased_side": "no"}}
    assert kalshiws.parse_message(msg)["side"] == "no"


@pytest.mark.parametrize("bad", [{"market_ticker": ""}, {"count_fp": "0"}, {"purchased_side": "maybe", "side": "maybe"}])
def test_an_unusable_fill_is_dropped(bad):
    assert kalshiws.parse_message({"type": "fill", "msg": {**FILL["msg"], **bad}}) is None


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def build(**over):
        s = replace(config.load(), **{"manual_exit": True, "manual_exit_skip": ("SENATE",), **over})
        w = watcher.ProfitWatcher(lambda: s, object(), lambda: Store(s.db_path))
        w.refreshed = 0
        w.refresh = lambda: setattr(w, "refreshed", w.refreshed + 1)  # type: ignore[method-assign]
        w.manual.invalidate = lambda: setattr(w, "invalidated", True)  # type: ignore[method-assign]
        w.followed = []
        w.follow = lambda tickers: w.followed.append(set(tickers))
        return w

    return build


def fill(ticker="KXBTC15M-1", action="buy", ts=None):
    return {"kind": "fill", "ticker": ticker, "side": "yes", "action": action, "count": 1.0, "price": 0.4, "ts": ts or time.time()}


def test_a_buy_waits_for_the_books_to_settle_then_enrolls_the_market(make):
    w = make()
    w.on_event(fill(ts=time.time()))
    assert w.enroll_fills() == 0 and w.followed == []  # too soon: the bot's own bet may not be recorded yet
    w.on_event(fill(ticker="KXETH15M-2", ts=time.time() - watcher.FILL_SETTLE - 1))
    assert w.enroll_fills() == 1
    assert w.followed == [{"KXETH15M-2"}] and w.invalidated and w.refreshed == 1


def test_one_refresh_covers_several_fills(make):
    w = make()
    old = time.time() - watcher.FILL_SETTLE - 1
    for t in ("KXBTC15M-1", "KXETH15M-2", "KXBTC15M-1"):
        w.on_event(fill(ticker=t, ts=old))
    assert w.enroll_fills() == 2 and w.refreshed == 1 and w.followed == [{"KXBTC15M-1", "KXETH15M-2"}]


def test_sells_and_skipped_prefixes_are_ignored(make):
    w = make()
    old = time.time() - watcher.FILL_SETTLE - 1
    w.on_event(fill(action="sell", ts=old))
    w.on_event(fill(ticker="SENATE-26-X", ts=old))
    assert w.enroll_fills() == 0 and w.refreshed == 0


def test_nothing_happens_with_manual_exit_off(make):
    w = make(manual_exit=False)
    w.on_event(fill(ts=time.time() - watcher.FILL_SETTLE - 1))
    assert w.enroll_fills() == 0 and w.refreshed == 0
