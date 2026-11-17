"""The buy's risk trend (lane fields entry_risk_trend, entry_risk_trend_live): 2026-11-17, user, on the entry trend check: "it shouldn't even care about the entry trend by itself. its the risk factor"; "i want to build something that replaces it"; "Exit's risk factor". The unit tests read curverisk alone; the integration tests go through a lane file and runner.evaluate with quotes and perp ticks in memory."""
import math
import time

import pytest

from kalshi_moonshot import curverisk, lanes, runner, ticks
from kalshi_moonshot.watcher import perp_index
from tests.test_flip_rule import decide, seed

STEP = curverisk.TREND_STEP


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.mids.clear()
    ticks.history.clear()
    runner._risk_marks.clear()
    runner._risk_seen.clear()
    runner._risk_seen_at.clear()


def marks(now, n, level=lambda k: 0.6, wobble=0.004):
    """n + 1 marks ending at `now`, one a step: level(k) for the k-th, with a wobble so the market has a typical move."""
    return [(now - (n - k) * STEP, level(k) + (wobble if k % 2 else -wobble)) for k in range(n + 1)]


def test_unit_a_risk_that_rose_reads_positive_and_one_that_fell_negative():
    now = 1_000_000.0
    up = curverisk.risk_trend(marks(now, 60, lambda k: 0.5 + 0.002 * k), now, 150.0)
    down = curverisk.risk_trend(marks(now, 60, lambda k: 0.7 - 0.002 * k), now, 150.0)
    assert up is not None and up[0] > 0 and up[1] > 0
    assert down is not None and down[0] < 0
    assert down[1] == pytest.approx(up[1])  # the same wobble, the same typical move


def test_unit_the_move_being_measured_is_not_part_of_its_own_typical_move():
    now = 1_000_000.0
    quiet = curverisk.risk_trend(marks(now, 60), now, 150.0)
    jumped = curverisk.risk_trend(marks(now, 60, lambda k: 0.6 if k <= 30 else 0.6 + 0.01 * (k - 30)), now, 150.0)  # flat, then 30 c inside the look back
    assert quiet is not None and jumped is not None
    assert jumped[1] == pytest.approx(quiet[1])  # the unit comes from before the look back only
    assert jumped[0] / jumped[1] > 5.0


def test_unit_the_typical_move_scales_with_the_look_back():
    now = 1_000_000.0
    m = marks(now, 120)
    short, long = curverisk.risk_trend(m, now, 50.0), curverisk.risk_trend(m, now, 200.0)
    assert short is not None and long is not None
    assert long[1] == pytest.approx(short[1] * 2.0)  # four times the span, twice the typical move


def test_unit_no_reading_when_the_market_is_young_flat_or_has_a_gap_at_either_end():
    now = 1_000_000.0
    assert curverisk.risk_trend(marks(now, 35), now, 150.0) is None  # 5 moves before the look back: under a minute of them
    assert curverisk.risk_trend(marks(now, 60, wobble=0.0), now, 150.0) is None  # never moved: no unit
    assert curverisk.risk_trend(marks(now - 30.0, 60), now, 150.0) is None  # the newest mark is 30 s old
    holed = [(t, None if abs(t - (now - 150.0)) < 1.0 else q) for t, q in marks(now, 60)]
    assert curverisk.risk_trend(holed, now, 150.0) is None  # the risk could not be read at the start of the look back
    assert curverisk.risk_trend([], now, 150.0) is None
    assert curverisk.risk_trend(marks(now, 60), now, 2.0) is None  # a look back under one step


def test_unit_the_state_is_the_direction_until_the_hour_gives_a_range():
    assert curverisk.trend_state(0.0, []) == "holding"  # not worse counts as holding
    assert curverisk.trend_state(1.5, []) == "holding"
    assert curverisk.trend_state(-0.1, []) == "worsening"
    assert curverisk.trend_state(40.0, [0.1 * k for k in range(50)]) == "holding"  # 50 readings: too few to call a range


def test_unit_a_reading_outside_the_hours_range_is_an_anomaly_either_way():
    seen = [-2.0 + 0.04 * k for k in range(101)]  # -2 to +2
    assert curverisk.trend_state(1.0, seen) == "holding"
    assert curverisk.trend_state(-1.0, seen) == "worsening"
    assert curverisk.trend_state(9.0, seen) == "anomaly"
    assert curverisk.trend_state(-9.0, seen) == "anomaly"


def quotes(now, opened, level):
    """(time, YES mid) rows, one a second from the open, level(seconds since the open) with a wobble."""
    return [(opened + k, level(k) + (0.004 if k % 2 else -0.004)) for k in range(int(now - opened) + 1)]


def perp(now, opened, after=lambda k: 0.0):
    """The Kalshi perp, one tick a second for 80 minutes ending now: a small zig-zag by the minute, plus after(seconds since the open) percent once the market is open."""
    ix = perp_index("KXBTC15M")
    ticks.history.clear()
    t = now - 4800.0
    while t <= now:
        pct = 0.01 * (1 if int((now - t) // 60) % 2 else -1) + (after(t - opened) if t >= opened else 0.0)
        ticks.history.note(ix, t, 100.0 * math.exp(pct / 100.0))
        t += 1.0


RISING = lambda k: 0.52 + 0.0003 * k  # noqa: E731
FALLING = lambda k: 0.70 - 0.0003 * k  # noqa: E731
FLAT = lambda k: 0.615  # noqa: E731
SHADOW = "entry_risk_trend=0.25"
LIVE = "entry_risk_trend=0.25\nentry_risk_trend_live=true"


def setup(level, after=lambda k: 0.5):
    now = time.time()
    opened = now - 300.0  # decide() gives the market 600 s to run
    seed(quotes(now, opened, level))
    perp(now, opened, after)
    return now


def test_integration_shadow_a_risk_that_improved_is_tagged_would_buy_and_the_bet_goes_in(tmp_path, monkeypatch):
    now = setup(RISING)
    out = decide(tmp_path, monkeypatch, now, SHADOW)
    assert out.action == "bet" and "[risk trend +" in out.note and "over 150s: would buy]" in out.note  # a quarter of the 600 s left


def test_integration_shadow_a_risk_that_worsened_is_tagged_would_not_buy_and_nothing_is_gated(tmp_path, monkeypatch):
    now = setup(FALLING)
    out = decide(tmp_path, monkeypatch, now, SHADOW)
    assert out.action == "bet" and "[risk trend -" in out.note and "would not buy]" in out.note


def test_integration_shadow_carries_the_old_price_trend_reading_beside_it(tmp_path, monkeypatch):
    now = setup(RISING)
    out = decide(tmp_path, monkeypatch, now, SHADOW + "\nentry_trend_seconds=15\nentry_trend_z=0.0")
    assert "[risk trend +" in out.note and "| price trend " in out.note


def test_integration_shadow_changes_no_decision(tmp_path, monkeypatch):
    for level in (RISING, FALLING):
        now = setup(level)
        with_it = decide(tmp_path, monkeypatch, now, SHADOW + "\nentry_trend_seconds=15\nentry_trend_z=0.0")
        without = decide(tmp_path, monkeypatch, now, "entry_trend_seconds=15\nentry_trend_z=0.0")
        assert with_it.action == without.action


def test_integration_the_long_layer_alone_can_worsen_the_risk_while_the_price_is_flat(tmp_path, monkeypatch):
    now = setup(FLAT, after=lambda k: 0.0 if k < 150 else -0.004 * (k - 150))  # the perp falls 0.6% through its open over the look back; Kalshi's price has not moved
    out = decide(tmp_path, monkeypatch, now, SHADOW)
    assert "[risk trend -" in out.note and "would not buy]" in out.note
    runner._risk_marks.clear()
    now = setup(FLAT)  # the same flat price with the perp well over its open: the risk is the price, and it held
    assert "would not buy" not in decide(tmp_path, monkeypatch, now, SHADOW).note


def test_integration_the_other_side_reads_the_mirror(tmp_path, monkeypatch):
    now = setup(FALLING, after=lambda k: -0.5)  # YES falling, the perp under its open: NO's risk is improving
    out = decide(tmp_path, monkeypatch, now, SHADOW, side="no")
    assert "[risk trend +" in out.note and "would buy]" in out.note


def test_integration_live_a_worsening_risk_is_not_bought_and_the_note_says_why(tmp_path, monkeypatch):
    now = setup(FALLING)
    out = decide(tmp_path, monkeypatch, now, LIVE)
    assert out.action == "pass" and "risk worsening" in out.note and "150s" in out.note


def test_integration_live_an_improving_risk_is_bought_and_the_old_check_no_longer_decides(tmp_path, monkeypatch):
    now = setup(RISING)
    old_alone = decide(tmp_path, monkeypatch, now, "entry_trend_seconds=15\nentry_trend_z=0.0")
    assert old_alone.action == "pass" and "no reference" in old_alone.note  # the old check has no tape here and refuses
    out = decide(tmp_path, monkeypatch, now, LIVE + "\nentry_trend_seconds=15\nentry_trend_z=0.0")
    assert out.action == "bet" and "[risk trend +" in out.note and "would" not in out.note


def test_integration_a_market_too_young_to_read_is_tagged_unreadable_and_live_does_not_buy_it(tmp_path, monkeypatch):
    now = time.time()
    opened = now - 100.0
    seed(quotes(now, opened, RISING))
    perp(now, opened, lambda k: 0.5)
    shadow = decide(tmp_path, monkeypatch, now, SHADOW, left=800.0)
    assert shadow.action == "bet" and "[risk trend unreadable]" in shadow.note
    live = decide(tmp_path, monkeypatch, now, LIVE, left=800.0)
    assert live.action == "pass" and "no risk trend reading" in live.note


def test_integration_no_long_layer_means_no_reading(tmp_path, monkeypatch):
    now = setup(RISING)
    ticks.history.clear()
    assert "[risk trend unreadable]" in decide(tmp_path, monkeypatch, now, SHADOW).note


def test_integration_live_an_anomaly_sits_out(tmp_path, monkeypatch):
    now = setup(RISING)
    runner._risk_seen["KXBTC15M"] = runner.deque((now - 10.0, -0.2 + 0.004 * k) for k in range(101))  # the hour's readings ran from -0.2 to +0.2
    out = decide(tmp_path, monkeypatch, now, LIVE)
    assert out.action == "pass" and "risk trend anomaly" in out.note


def test_the_lane_version_changes_only_when_the_gate_is_live(tmp_path, monkeypatch):
    from kalshi_moonshot import config
    from tests.test_flip_rule import LANE

    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def version(extra):
        (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
        s = config.load()
        return lanes.settings_of(lanes.spec_for(s, "fl"), s)

    assert version(SHADOW) == version("")  # shadow changes nothing the lane does
    assert version(LIVE) != version("")
