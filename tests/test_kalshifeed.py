"""Kalshi's own perp reference, read from the tape, as a price feed for the ladder pricer."""
import time

import pytest

from kalshi_moonshot import commodities as cm
from kalshi_moonshot.kalshifeed import KalshiPerpFeed
from kalshi_moonshot.krakenfeed import RoutingFeed
from kalshi_moonshot.tape import Tape


def _tape(tmp_path, rows):
    tape = Tape(tmp_path / "tape.db")
    tape.db.executemany("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", [(t, i, v, None, None, None) for t, i, v in rows])
    tape.db.commit()
    return tape


def _ticks(index_id="KP_BTC", minutes=40, start_value=100.0):
    now = time.time()
    n = minutes * 60
    return [(now - (n - k), index_id, start_value + 0.01 * k) for k in range(n)]  # one reading a second, the newest a second old


def test_series_has_the_newest_tick_and_five_minute_closes(tmp_path):
    rows = _ticks()
    feed = KalshiPerpFeed(_tape(tmp_path, rows).db)
    s = feed.series("kp:KP_BTC")
    assert s.last == pytest.approx(rows[-1][2]) and s.last_ts == pytest.approx(rows[-1][0])
    assert s.times == sorted(s.times) and all(t % 300 == 0 for t in s.times) and 8 <= len(s.closes) <= 10 and len(s.closes) == len(s.times)
    assert s.closes[-1] == pytest.approx(rows[-1][2])  # the newest bar closes on the newest tick
    done = len(s.times) - 2  # a finished bar closes on the last tick inside it
    inside = [v for t, _, v in rows if s.times[done] <= t < s.times[done] + 300]
    assert s.closes[done] == pytest.approx(inside[-1])


def test_minute_closes_are_keyed_by_the_second_the_minute_opened(tmp_path):
    rows = _ticks()
    out = KalshiPerpFeed(_tape(tmp_path, rows).db).minute_closes("kp:KP_BTC", hours=1.0)
    assert out and all(k % 60 == 0 for k in out) and all(isinstance(k, int) for k in out)
    k = sorted(out)[-2]
    assert out[k] == pytest.approx([v for t, _, v in rows if k <= t < k + 60][-1])


def test_a_coin_with_no_recorded_ticks_cannot_be_priced(tmp_path):
    feed = KalshiPerpFeed(_tape(tmp_path, _ticks("KP_ETH")).db)
    with pytest.raises(cm.CommodityError):
        feed.series("kp:KP_BTC")


def test_a_reading_of_zero_is_not_a_price(tmp_path):
    rows = _ticks()
    rows.append((rows[-1][0] + 0.5, "KP_BTC", 0.0))
    s = KalshiPerpFeed(_tape(tmp_path, rows).db).series("kp:KP_BTC")
    assert s.last == pytest.approx(rows[-2][2])


def test_a_tape_that_cannot_be_read_is_a_pricing_error():
    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("connection lost")

    with pytest.raises(cm.CommodityError):
        KalshiPerpFeed(Broken()).series("kp:KP_BTC")


def test_the_router_sends_kp_symbols_to_the_kalshi_feed(tmp_path):
    rows = _ticks()
    router = RoutingFeed(kraken=object(), kalshi=KalshiPerpFeed(_tape(tmp_path, rows).db))
    assert router.series("kp:KP_BTC").last == pytest.approx(rows[-1][2])
    assert router.minute_closes("kp:KP_BTC", 1.0)


def test_the_crypto_instruments_read_kalshis_perp_and_the_currencies_still_read_kraken():
    by = {i.prefix: i for i in cm.load_instruments()}
    for coin in ("BTC", "ETH", "SOL", "XRP", "DOGE"):
        inst = by[f"KX{coin}"]
        assert inst.symbol == f"kp:KP_{coin}" and inst.proxy and inst.realtime and not cm.contract_priced(inst.symbol)
    assert cm.contract_priced(by["KXAUDUSD"].symbol)


def test_a_kalshi_perp_is_not_a_feed_ticks_symbol():
    from kalshi_moonshot.runner import _spot_symbols

    assert _spot_symbols("KXBTC15M") == []
