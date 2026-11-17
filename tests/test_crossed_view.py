"""The never-crossed reading, shadow only (lane field entry_crossed_view): 2026-11-17, user: "Yes, shadow it", on "no buy of a side that never crossed 50c"; studies/results/2026-11-17-buy-shapes.txt."""
import time

import pytest

from kalshi_moonshot import config, lanes, ticks, variants
from tests.test_flip_rule import LANE, decide, seed


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.mids.clear()
    ticks.history.clear()


def steady(now, opened, mid=0.62):
    """YES the favorite from the open, one quote a second, never under even."""
    return [(opened + k, mid + (0.004 if k % 2 else -0.004)) for k in range(int(now - opened) + 1)]


def dipped(now, opened, dips=1):
    """YES the favorite, with `dips` 20-second stretches under even that come back."""
    n = int(now - opened)
    under = set()
    for d in range(dips):
        start = 60 + 60 * d
        under |= set(range(start, start + 20))
    return [(opened + k, 0.44 if k in under else 0.62) for k in range(n + 1)]


def test_unit_the_count_is_the_crossings_of_even_since_the_open_on_a_5_second_grid():
    now = time.time()
    opened = now - 300.0
    assert variants.quote_crossings(steady(now, opened), opened, now) == 0
    assert variants.quote_crossings(dipped(now, opened, 1), opened, now) == 2  # under and back over
    assert variants.quote_crossings(dipped(now, opened, 2), opened, now) == 4
    assert variants.quote_crossings([(t, 1.0 - m) for t, m in dipped(now, opened, 1)], opened, now) == 2  # the same count read from the other side


def test_unit_only_this_market_counts_and_a_young_or_stale_one_has_no_reading():
    now = time.time()
    opened = now - 300.0
    before = [(opened - 200.0 + k, 0.30) for k in range(200)]  # under even before the open: not this market
    assert variants.quote_crossings(before + steady(now, opened), opened, now) == 0
    assert variants.quote_crossings(steady(now, now - 100.0), now - 100.0, now) is None  # under three minutes of quotes
    assert variants.quote_crossings(steady(now - 30.0, opened), opened, now) is None  # the newest quote is 30 s old
    assert variants.quote_crossings([], opened, now) is None


def test_integration_a_favorite_never_tested_is_still_bought_and_the_note_says_would_not_buy(tmp_path, monkeypatch):
    now = time.time()
    seed(steady(now, now - 300.0))
    out = decide(tmp_path, monkeypatch, now, "entry_crossed_view=true")
    assert out.action == "bet" and "[never crossed: would not buy]" in out.note  # shadow: the buy goes through


def test_integration_a_favorite_that_was_tested_and_came_back_carries_its_count(tmp_path, monkeypatch):
    now = time.time()
    seed(dipped(now, now - 300.0))
    out = decide(tmp_path, monkeypatch, now, "entry_crossed_view=true")
    assert out.action == "bet" and "[crossed 2]" in out.note and "never crossed" not in out.note


def test_integration_without_the_setting_the_note_says_nothing(tmp_path, monkeypatch):
    now = time.time()
    seed(steady(now, now - 300.0))
    out = decide(tmp_path, monkeypatch, now, "")
    assert out.action == "bet" and "crossed" not in out.note


def test_integration_a_market_too_young_to_read_is_marked_unreadable_and_still_bought(tmp_path, monkeypatch):
    now = time.time()
    seed(steady(now, now - 100.0))
    out = decide(tmp_path, monkeypatch, now, "entry_crossed_view=true", left=800.0)
    assert out.action == "bet" and "[crossed view unreadable]" in out.note


def test_integration_the_reading_rides_on_a_pass_too(tmp_path, monkeypatch):
    now = time.time()
    seed(steady(now, now - 300.0))
    out = decide(tmp_path, monkeypatch, now, "entry_crossed_view=true\nentry_led_max=0.2")  # the led gate refuses a side that led all market
    assert out.action == "pass" and "[never crossed: would not buy]" in out.note


def test_unit_the_view_is_shadow_and_never_changes_a_lanes_version(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seen = []
    for extra in ("", "entry_crossed_view=true"):
        (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
        s = config.load()
        spec = lanes.lane_specs(s)["fl"]
        assert spec.entry_crossed_view is bool(extra)
        seen.append(lanes.settings_of(spec, s))
    assert seen[0] == seen[1]


def test_integration_the_tag_leads_the_note_so_the_300_character_cut_keeps_it(tmp_path, monkeypatch):
    """2026-11-17: the first deploy put the tag behind the spike and scenario tags, and the stored reason (cut at 300 characters) lost it on every decision."""
    now = time.time()
    seed(steady(now, now - 300.0))
    out = decide(tmp_path, monkeypatch, now, "entry_crossed_view=true\nentry_spike_view=true")
    assert out.note.startswith("[never crossed: would not buy] ")
    from kalshi_moonshot.store import Store

    row = Store(config.load().db_path).db.execute("SELECT reason FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["reason"].startswith("[never crossed: would not buy] ")
