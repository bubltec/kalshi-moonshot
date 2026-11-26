"""The hour view: built in the background every five minutes for every active market, read from memory (hourview.py), a fact on the board, logged on a PriskP buy."""
import math
import time
from types import SimpleNamespace

import pytest

from kalshi_moonshot import facts, hourview, memguard


@pytest.fixture(autouse=True)
def clean():
    hourview.clear()
    yield
    hourview.clear()


def ticks(now, drift_per_hour=0.012, seconds=4000, wobble=0.0002):
    """One-second ticks of an index that rises `drift_per_hour` (log) over the hour, with a small wobble."""
    T = [now - seconds + k for k in range(seconds + 1)]
    V = [100.0 * math.exp(drift_per_hour * (t - (now - 3600.0)) / 3600.0 + wobble * (-1) ** k) for k, t in enumerate(T)]
    return T, V


def test_build_reads_the_hour_in_the_markets_own_units():
    now = 1_900_000_000.0
    T, V = ticks(now)
    v = hourview.build_one(T, V, now, 100.0)
    assert v is not None and v["drift_yes"] > 1.0 and v["eff"] > 0 and v["cross"] >= 0 and v["range"] >= v["drift_yes"] - 1e-9
    assert hourview.build_one(T[-500:], V[-500:], now, 100.0) is None  # too thin an hour: no view
    assert hourview.build_one(T, V, now, 0.0) is None  # no strike


def test_read_is_for_the_side_and_goes_stale():
    now = 1_900_000_000.0
    T, V = ticks(now)
    hourview._VIEW["KXBTC15M-X-00"] = dict(hourview.build_one(T, V, now, 100.0), built_at=now)
    yes = hourview.read("KXBTC15M-X-00", "yes", now + 10)
    no = hourview.read("KXBTC15M-X-00", "no", now + 10)
    assert yes["drift"] > 0 > no["drift"] and yes["drift"] == pytest.approx(-no["drift"])  # an hour up is with a YES and against a NO
    assert yes["hi_ago"] == pytest.approx(no["lo_ago"]) and yes["age"] == 10
    assert hourview.read("KXBTC15M-X-00", "yes", now + 2 * hourview.EVERY + 1) is None  # older than twice the rebuild: nothing said
    assert hourview.read("KXOTHER15M-X-00", "yes", now) is None


def test_the_background_job_builds_every_five_minutes_from_memory(monkeypatch):
    from kalshi_moonshot import binaryrisk
    from kalshi_moonshot import ticks as ticks_mod

    now = 1_900_000_000.0
    ticks_mod.history.clear()
    T, V = ticks(now)
    ticks_mod.history.warm("BRTI", list(zip(T, V, strict=True)), T[0] - 1.0)
    monkeypatch.setattr(ticks_mod, "recent_15m_tickers", lambda s, n, hours=1.0: ["KXBTC15M-26NOV231700-00"])
    monkeypatch.setattr(binaryrisk, "curve_index", lambda series: "BRTI")
    ticks_mod.strikes.note("KXBTC15M-26NOV231700-00", 100.0)
    assert hourview.maybe_refresh(None, now) is True
    assert hourview.read("KXBTC15M-26NOV231700-00", "yes", now + 1)["drift"] > 1.0
    assert hourview.maybe_refresh(None, now + 100) is False  # not five minutes yet
    assert hourview.maybe_refresh(None, now + 301) is True
    ticks_mod.history.clear()
    ticks_mod.strikes._v.clear()


def test_the_board_reads_it_as_a_fact_with_no_database_call():
    now = time.time()
    T, V = ticks(now)
    hourview._VIEW["KXBTC15M-26NOV231700-00"] = dict(hourview.build_one(T, V, now, 100.0), built_at=now)
    t = SimpleNamespace(ticker="KXBTC15M-26NOV231700-00", side="no", lane="priskp")
    b = facts.Board(SimpleNamespace(), t, now, 600.0, 0.8)
    with memguard.decision("buy"):
        got = b.view("priskp").get("hour_view")
    assert got["drift"] < 0  # a NO into a rising hour
    assert "hour_view={drift=" in b.note()  # it is on the line the buy writes
    assert facts.NEEDS["priskp"] == ("priskp_screen", "shift_out", "hour_view", "price_gap", "waves_before", "wave_now", "wave_leg")  # the wave facts (2026-11-25) are on the board for every step to read


def test_the_hour_view_reads_a_bounce_in_a_falling_hour_and_flags_it_for_the_shadow():
    now = 1_900_000_000.0
    T = [now - 4000 + k for k in range(4001)]
    # a falling hour (lower highs every 20 minutes) that bounces in its last five minutes
    def level(t):
        m = (t - (now - 3600.0)) / 60.0
        base = 100.0 - 0.004 * m
        return base + 0.02 * math.sin(m / 3.2) + (0.03 if m > 54 else 0.0)

    V = [level(t) for t in T]
    v = hourview.build_one(T, V, now, 100.0)
    assert v is not None and len(v["bins_yes"]) == 12
    hourview._VIEW["KXBTC15M-X-00"] = dict(v, built_at=now)
    yes = hourview.read("KXBTC15M-X-00", "yes", now + 5)
    assert yes["drift"] < 0 and yes["bounce"] > 0 and len(yes["bins"]) == 12
    no = hourview.read("KXBTC15M-X-00", "no", now + 5)
    assert no["bounce"] == pytest.approx(-yes["bounce"])  # a bounce up is a drop for the other side
    assert hourview.would_skip({"drift": -5.0, "bounce": 1.2}) and not hourview.would_skip({"drift": -5.0, "bounce": 0.2}) and not hourview.would_skip({"drift": 1.0, "bounce": 2.0})
    assert hourview.lower_highs([1.0, 3.0, 1.0, 2.0, 0.5, 1.4, 0.0, 0.8, -0.5, 0.2, -1.0, -0.8]) and not hourview.lower_highs([1.0, 3.0, 1.0, 3.2, 0.5, 4.0, 0.0])
