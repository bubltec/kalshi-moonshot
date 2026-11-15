"""The recorder's memory report (2026-11-15): it reads every in-memory store the recorder feeds and says what grew; it changes nothing."""
from types import SimpleNamespace

from kalshi_moonshot import recorderdiag, tape as tape_mod, ticks as ticks_mod


def test_sizes_counts_every_store_the_recorder_feeds():
    ticks_mod.mids._rows.clear()
    tape_mod.recent.clear()
    ticks_mod.mids.note("KXBTC15M-1", 100.0, 0.50, 0.52)
    ticks_mod.mids.note("KXBTC15M-1", 101.0, 0.51, 0.53)
    tape_mod.recent.note_quote("KXETH15M-1", 100.0, 0.40, 0.42)
    rec = SimpleNamespace(tape=SimpleNamespace(_quotes=[1, 2, 3], _index=[], _feed=[], _flow={}, _fast=[1]), stream=SimpleNamespace(_tickers={"a": 1, "b": 2}))
    line = recorderdiag.sizes(rec)
    assert "mids 2 tickers" in line and "history" in line and "writer queues _quotes 3" in line and "_fast 1" in line and "stream following 2 tickers" in line
    ticks_mod.mids._rows.clear()
    tape_mod.recent.clear()


def test_growth_names_the_allocation_that_grew_once_tracing_is_on():
    assert recorderdiag.growth() == "tracemalloc off"
    recorderdiag.start_tracing()
    try:
        assert recorderdiag.growth().startswith("baseline taken")
        pile = [bytearray(1000) for _ in range(2000)]  # about 2 MB that stays alive
        out = recorderdiag.growth()
        assert "grew since the baseline" in out and "MiB" in out
        del pile
    finally:
        import tracemalloc

        tracemalloc.stop()
        recorderdiag._baseline = None


def test_object_types_lists_the_most_numerous_types():
    assert "," in recorderdiag.object_types(3)
