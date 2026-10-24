import json
import math
import random
import sqlite3
import time

from kalshi_moonshot import config, kalshiws, leadlag
from kalshi_moonshot.tape import Tape, TapeRecorder

QUOTE = {"type": "ticker", "sid": 1, "msg": {
    "market_ticker": "KXBTC15M-26OCT192030-30", "price_dollars": "0.4700", "yes_bid_dollars": "0.4600",
    "yes_ask_dollars": "0.4700", "volume_fp": "5000"}, "sending_ts_ms": 1792455376232}
INDEX = {"type": "cfbenchmarks_value", "sid": 2, "msg": {
    "index_id": "BRTI", "data": json.dumps({"type": "value", "time": 1792455441000, "id": "BRTI", "value": "81584.08"}),
    "avg_60s_data": {"value": "81580.50000000", "window_size": 12},
    "last_60s_windowed_average_15min": {"value": "81570.00"}}, "sending_ts_ms": 1792455441064}


def test_parse_quote_and_index_messages():
    q = kalshiws.parse_message(QUOTE, received=100.0)
    assert (q["kind"], q["ticker"], q["bid"], q["ask"], q["last"]) == ("quote", "KXBTC15M-26OCT192030-30", 0.46, 0.47, 0.47)
    i = kalshiws.parse_message(INDEX, received=100.0)
    assert (i["kind"], i["index_id"], i["value"], i["avg60"], i["quarter_avg"]) == ("index", "BRTI", 81584.08, 81580.5, 81570.0)


def test_other_messages_are_ignored():
    assert kalshiws.parse_message({"type": "subscribed", "id": 1}) is None
    assert kalshiws.parse_message({"type": "ticker", "msg": {"market_ticker": "X"}}) is None  # no quote
    assert kalshiws.parse_message({"type": "cfbenchmarks_value", "msg": {"index_id": "BRTI", "data": "not json"}}) is None


def test_series_map_to_their_settlement_index():
    assert kalshiws.index_for_series("KXBTC15M") == "BRTI"
    assert kalshiws.index_for_series("KXETHD") == "ETHUSD_RTI"
    assert kalshiws.index_for_series("KXGOLD15M") is None  # commodities settle on Pyth, not CF Benchmarks
    assert kalshiws.index_for_series("KXBTCMAXD") is None


def test_the_tape_skips_repeated_quotes_and_flushes_in_batches(tmp_path):
    tape = Tape(tmp_path / "tape.db")
    q = {"kind": "quote", "ts": 1.0, "ticker": "T", "bid": 0.4, "ask": 0.5, "last": 0.45, "sts": 1}
    tape.add(q)
    tape.add({**q, "ts": 2.0})  # same bid and ask: nothing new
    tape.add({**q, "ts": 3.0, "bid": 0.41})
    tape.add({"kind": "index", "ts": 1.0, "index_id": "BRTI", "value": 1.0, "avg60": 1.0, "quarter_avg": None, "src_ms": 1})
    tape.add_feed("hl:xyz:GOLD", 4100.0, 10.0)
    tape.add_feed("hl:xyz:GOLD", 4100.0, 11.0)  # unchanged price
    tape.add_feed("hl:xyz:GOLD", 4101.0, 10.2)  # too soon after the last stored one
    tape.add_feed("hl:xyz:GOLD", 4101.0, 12.0)
    assert tape.flush() == 5
    assert tape.flush() == 0
    assert tape.db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 2


def test_old_tape_rows_are_pruned(tmp_path):
    tape = Tape(tmp_path / "tape.db")
    tape.add({"kind": "index", "ts": 100.0, "index_id": "BRTI", "value": 1.0, "avg60": 1.0, "quarter_avg": None, "src_ms": 1})
    tape.add({"kind": "index", "ts": time.time(), "index_id": "BRTI", "value": 1.0, "avg60": 1.0, "quarter_avg": None, "src_ms": 1})
    tape.flush()
    tape.prune(keep_days=3)
    assert tape.db.execute("SELECT COUNT(*) FROM index_ticks").fetchone()[0] == 1


def test_the_recorder_follows_the_open_15_minute_markets(tmp_path):
    s = config.load()
    s = type(s)(**{**s.__dict__, "data_dir": tmp_path})

    class Client:
        authed = True

        def series_markets(self, series):
            return [{"ticker": f"{series}-26OCT192030-30", "status": "active"},
                    {"ticker": f"{series}-OLD", "status": "closed"}]

    rec = TapeRecorder(Client(), s, None)  # type: ignore[arg-type]
    n = rec.refresh_tickers()
    assert n == 20 and all(t.endswith("-26OCT192030-30") for t in rec.stream._tickers)
    assert "hl:xyz:GOLD" in rec._commodity_symbols() and not any("BTC" in x for x in rec._commodity_symbols())


# ---- the analysis ----
def make_world(lag, seconds=1800, seed=3):
    """A feed that random-walks, and a quote that copies it `lag` seconds late (in contract-price terms)."""
    rng = random.Random(seed)
    px, feed = 100.0, []
    for _ in range(seconds + lag + 5):
        px *= math.exp(rng.gauss(0, 0.0004))
        feed.append(px)
    mid = [0.5 + (feed[i] / 100.0 - 1) * 40 for i in range(len(feed))]
    mid = [min(0.95, max(0.05, m)) for m in mid]
    quote = [mid[max(0, i - lag)] for i in range(seconds)]
    feed = feed[:seconds]
    return leadlag.Track("T", 0, [q - 0.005 for q in quote], [q + 0.005 for q in quote], feed)


def test_the_correlation_peaks_at_the_quotes_delay():
    corr = leadlag.correlation_by_lag([make_world(lag=6)], tuple(range(-3, 16)))
    best = max(corr, key=lambda lag: corr[lag])
    assert best == 6 and corr[6] > 0.5
    assert corr[0] < 0.2  # not instantaneous


def test_a_quote_that_follows_at_once_peaks_at_zero():
    corr = leadlag.correlation_by_lag([make_world(lag=0)], tuple(range(-3, 16)))
    assert max(corr, key=lambda lag: corr[lag]) == 0


def test_jumps_are_found_with_their_direction():
    f = [100.0] * 60 + [100.0 + 0.05 * i for i in range(1, 6)] + [100.25] * 60
    t = leadlag.Track("T", 0, [0.49] * len(f), [0.51] * len(f), f)
    found = leadlag.jumps(t, z=2.0, min_bp=1.0)
    assert found and found[0][1] == 1 and 60 <= found[0][0] <= 66


def test_a_lagging_quote_drifts_after_a_jump_and_the_control_does_not():
    tracks = [make_world(lag=8, seed=s) for s in range(6)]
    resp = {r.horizon: r for r in leadlag.event_study(tracks)}
    assert resp[5].drift > resp[5].control + 0.002  # the quote keeps moving the jump's way
    assert resp[60].n > 10


def test_the_trade_test_pays_when_the_quote_lags_and_not_when_it_follows_at_once():
    lagging = leadlag.trade_test([make_world(lag=8, seed=s) for s in range(8)], latency=2, holds=(10,))[0]
    instant = leadlag.trade_test([make_world(lag=0, seed=s) for s in range(8)], latency=2, holds=(10,))[0]
    assert lagging.n > 5 and lagging.avg_cents > instant.avg_cents + 0.5
    assert instant.avg_cents < 0.5  # fees and the spread eat an instant quote's moves


def test_build_tracks_aligns_quotes_and_the_index(tmp_path):
    db = sqlite3.connect(":memory:")
    from kalshi_moonshot.tape import SCHEMA

    db.executescript(SCHEMA)
    for s in range(240):
        db.execute("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", (1000.0 + s, "BRTI", 80000 + s, 80000, None, 0))
        if s % 2 == 0:
            db.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?)", (1000.0 + s, "KXBTC15M-A-30", 0.4 + s / 1000, 0.41 + s / 1000, 0.4, 0))
    tracks = leadlag.build_tracks(db, "KXBTC15M", tmp_path)
    assert len(tracks) == 1 and len(tracks[0].bid) > 200
    assert len({len(tracks[0].bid), len(tracks[0].ask), len(tracks[0].feed)}) == 1
    assert leadlag.build_tracks(db, "KXETH15M", tmp_path) == []



def test_the_recorder_stores_kraken_prices_for_the_crypto_markets_only(tmp_path):
    s = config.load()
    s = type(s)(**{**s.__dict__, "data_dir": tmp_path})

    class Kraken:
        def latest(self, symbol):
            return (80000.0, 100.0) if symbol == "kraken:BTC/USD" else None  # the others have not ticked yet

    class Feed:
        kraken = Kraken()

    rec = TapeRecorder(object(), s, Feed())  # type: ignore[arg-type]
    assert "kraken:BTC/USD" in rec._crypto_symbols() and not any("PAXG" in x or "XAUT" in x for x in rec._crypto_symbols())
    rec.sample_kraken()
    rec.tape.flush()
    assert rec.tape.db.execute("SELECT symbol, px FROM feed_ticks").fetchall()[0][:] == ("kraken:BTC/USD", 80000.0)
