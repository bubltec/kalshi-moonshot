"""The Kalshi-first spot reading (variants.spot_index, lane field spot_source = "kalshi")."""

import time

from kalshi_moonshot import perpref, ticks, variants


def test_a_perp_reference_follows_each_commodity_and_the_index_series():
    assert perpref.index_for_series("KXGOLD15M") == "KP_GOLD" and perpref.index_for_series("KXINX15M") == "KP_SPX"
    assert perpref.index_for_series("KXNATGAS15M") == "KP_NATGAS"
    assert perpref.index_for_series("KXBTC15M") is None  # a coin's settlement index is already Kalshi's own
    assert perpref.index_for_series("KXGOLD") is None


def test_the_default_reading_is_unchanged_and_the_kalshi_reading_prefers_the_perp_reference():
    now = time.time()
    ticks.history.clear()
    try:
        ticks.history.note("KP_GOLD", now - 60.0, 4199.0)  # the history has to reach back past the freshness window
        ticks.history.note("KP_GOLD", now - 5.0, 4200.0)
        assert variants.spot_index("KXGOLD15M", "", now) == "BQ_GOLD"
        assert variants.spot_index("KXGOLD15M", "kalshi", now) == "KP_GOLD"
        assert variants.spot_index("KXINX15M", "kalshi", now) == "BQ_SPX"  # no perp tick: the external feed is the fallback
        assert variants.spot_index("KXBTC15M", "kalshi", now) == variants.crypto_index("KXBTC15M")  # a coin stays on Kalshi's settlement index
    finally:
        ticks.history.clear()


def test_a_stale_perp_reference_is_not_a_live_spot():
    now = time.time()
    ticks.history.clear()
    try:
        ticks.history.note("KP_GOLD", now - 120.0, 4200.0)
        assert variants.spot_index("KXGOLD15M", "kalshi", now) == "BQ_GOLD"
    finally:
        ticks.history.clear()

