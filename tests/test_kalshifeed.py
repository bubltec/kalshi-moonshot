"""Kalshi's own perp reference, read from the tape, as a price feed for the ladder pricer."""
import time

import pytest

from kalshi_moonshot import commodities as cm
from kalshi_moonshot.kalshifeed import KalshiPerpFeed
from kalshi_moonshot.routingfeed import RoutingFeed
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
    router = RoutingFeed(kalshi=KalshiPerpFeed(_tape(tmp_path, rows).db))
    assert router.series("kp:KP_BTC").last == pytest.approx(rows[-1][2])
    assert router.minute_closes("kp:KP_BTC", 1.0)


def test_the_crypto_instruments_read_kalshis_own_index():
    from kalshi_moonshot import kalshiws

    by = {i.prefix: i for i in cm.load_instruments()}
    for coin in ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BCH", "BNB", "HYPE", "NEAR", "ZEC"):  # all eleven coins (the last six added 2026-11-13)
        inst = by[f"KX{coin}"]
        assert inst.symbol == f"kp:{kalshiws.INDEX_FOR[f'KX{coin}']}" and inst.proxy and inst.realtime and not cm.contract_priced(inst.symbol)  # the settlement index the socket streams (2026-11-13; the polled perp reference before)
    assert "KXAUDUSD" not in by and "KXUSDCAD" not in by  # their Kraken spot feed was dropped 2026-11-14


def test_a_kalshi_perp_is_not_a_feed_ticks_symbol():
    from kalshi_moonshot.runner import _spot_symbols

    assert _spot_symbols("KXBTC15M") == []


# ---- the newest price comes from the websocket's in-memory ticks, not a poll (2026-11-13) ----

def test_latest_is_the_tick_the_socket_just_delivered_when_memory_has_a_fresh_one(tmp_path, monkeypatch):
    from kalshi_moonshot import ticks as ticks_mod

    fresh = ticks_mod.TickHistory()
    monkeypatch.setattr(ticks_mod, "history", fresh)
    now = time.time()
    rows = _ticks(index_id="BRTI")  # the tape's newest reading is a second old and reads about 124
    feed = KalshiPerpFeed(_tape(tmp_path, rows).db)
    fresh.warm("BRTI", [(now - 30.0, 500.0), (now - 0.2, 501.5)], now - 60.0)  # the socket's newest is 0.2 s old
    price, at = feed.latest("kp:BRTI")
    assert price == 501.5 and at == pytest.approx(now - 0.2, abs=0.01)
    assert feed.series("kp:BRTI").last == 501.5  # the ladder prices off it; the bars still come from the tape
    assert len(feed.series("kp:BRTI").closes) >= 8


def test_latest_falls_back_to_the_tape_when_memory_is_empty_or_stale(tmp_path, monkeypatch):
    from kalshi_moonshot import ticks as ticks_mod

    fresh = ticks_mod.TickHistory()
    monkeypatch.setattr(ticks_mod, "history", fresh)
    rows = _ticks(index_id="BRTI")
    feed = KalshiPerpFeed(_tape(tmp_path, rows).db)
    assert feed.latest("kp:BRTI")[0] == pytest.approx(rows[-1][2])  # nothing in memory
    now = time.time()
    fresh.warm("BRTI", [(now - 300.0, 500.0), (now - 60.0, 501.5)], now - 400.0)  # the socket stalled a minute ago
    assert feed.latest("kp:BRTI")[0] == pytest.approx(rows[-1][2])


def test_every_series_the_live_ladder_lane_lists_has_an_instrument_the_ladder_can_price():
    """Guards the lane file: a series with no instrument drops out as "could not price this ladder", which is how six coins were silently missing from the ladder lane."""
    import tomllib
    from pathlib import Path

    toml = Path(__file__).resolve().parents[1] / "data" / "strategies.toml"
    if not toml.exists():
        pytest.skip("no local lane file")
    lane = tomllib.loads(toml.read_text()).get("lane", {}).get("crypto_ladder_perp")
    if not lane:
        pytest.skip("no crypto_ladder_perp lane")
    missing = [s for s in lane["series"] if cm.symbol_for(s) is None or not cm.symbol_for(s).startswith("kp:")]
    assert missing == [] and len(lane["series"]) == 11
