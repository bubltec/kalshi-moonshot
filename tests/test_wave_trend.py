"""The shadow trend reading of the wave-top lane (wavetrend.py; 2026-11-22, user: the trend "shouldn't be a hard NO", "3 hours is more likely to show a general shift", "since we're betting against the trend should go against as well"): the template a background job builds from the tape, the signed trend, the nudge, and the buy step that only writes them into the note."""
import math

import pytest

from kalshi_moonshot import buychain, config, db, wavetrend
from kalshi_moonshot import ticks as ticks_mod
from tests.test_wave_top import INDEX, OPEN, TICKER, TOML, background, feed, mem, segments, shot  # noqa: F401  (mem: the fixture)


def tape_with_minutes(tmp_path, monkeypatch, hours=26, per_minute=lambda ts: 100.0):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS index_ticks (ts REAL NOT NULL, index_id TEXT NOT NULL, value REAL, avg60 REAL, quarter_avg REAL, src_ms REAL)")
    now = OPEN + 600.0
    start = int(now - hours * 3600) // 60 * 60
    for ts in range(start, int(now) + 1, 30):  # a tick every 30 s: half of them are on a minute
        tape.execute("INSERT INTO index_ticks (ts, index_id, value) VALUES (?, ?, ?)", (float(ts), INDEX, per_minute(ts)))
    tape.commit()
    return s, tape, now


@pytest.fixture(autouse=True)
def clean():
    wavetrend.clear()
    yield
    wavetrend.clear()


def test_the_template_holds_one_sample_a_minute_and_answers_by_the_sample_before_the_time(tmp_path, monkeypatch, mem):  # noqa: F811
    s, tape, now = tape_with_minutes(tmp_path, monkeypatch, per_minute=lambda ts: 100.0 + ts / 3600.0 - OPEN / 3600.0)
    n = wavetrend.refresh(s, [INDEX, INDEX], tape, now)
    assert 1500 <= n <= 1620  # 26 hours of minutes, once for the index named twice
    day_ago = OPEN - 24 * 3600
    assert wavetrend.value_at(INDEX, day_ago + 30) == pytest.approx(100.0 - 24.0, abs=0.01)  # the sample at the minute before
    assert wavetrend.value_at(INDEX, OPEN - 40 * 3600) is None  # older than the template
    assert wavetrend.value_at("NOSUCH", day_ago) is None
    assert wavetrend.refresh(s, [INDEX], None, now) == 0  # no tape: nothing, no error


def test_the_trend_is_the_drift_against_a_random_walks_spread_in_the_markets_own_units(tmp_path, monkeypatch, mem):  # noqa: F811
    s, tape, now = tape_with_minutes(tmp_path, monkeypatch, per_minute=lambda ts: 100.0 + (ts - OPEN) / 3600.0)  # a rise of 1 an hour
    wavetrend.refresh(s, [INDEX], tape, now)
    got = wavetrend.trends(INDEX, OPEN, 100.0, 0.5)  # the spot at the open is 100 and its typical minute 0.5
    assert got[24.0] == pytest.approx(24.0 / (0.5 * math.sqrt(60 * 24)), abs=0.01) and got[12.0] == pytest.approx(12.0 / (0.5 * math.sqrt(60 * 12)), abs=0.01)
    assert got[1.0] == pytest.approx(1.0 / (0.5 * math.sqrt(60)), abs=0.01)  # an hour back: the template reaches it as well
    assert wavetrend.trends(INDEX, OPEN, 100.0, 0.0) == {}  # no unit, no trend


def test_signed_for_the_bet_a_rise_runs_a_yes_bets_way_and_a_nos_against_it_and_the_nudge_follows():
    assert wavetrend.for_bet(2.0, "yes") == 2.0 and wavetrend.for_bet(2.0, "no") == -2.0
    assert wavetrend.nudge(-2.0) == pytest.approx(0.0472) and wavetrend.nudge(2.0) == pytest.approx(-0.0472)  # against the trend the bet is worth more, with it less
    assert wavetrend.nudge(-9.0) == pytest.approx(0.0708) and wavetrend.nudge(0.0) == 0.0  # clipped to three units


def test_the_note_reads_memory_signs_the_trend_for_the_bet_and_never_raises(tmp_path, monkeypatch, mem):  # noqa: F811
    s, tape, now = tape_with_minutes(tmp_path, monkeypatch, per_minute=lambda ts: 100.0 - (ts - OPEN) / 3600.0)  # a fall of 1 an hour into the open
    wavetrend.refresh(s, [INDEX], tape, now)
    assert "no tick history" in wavetrend.note(INDEX, OPEN, OPEN + 100, "yes")
    T, V, v = background(lead=4000)
    T, V, last = segments(T, V, v, [(100, 0.0)])
    feed(T, V)
    yes, no = wavetrend.note(INDEX, OPEN, OPEN + 100, "yes"), wavetrend.note(INDEX, OPEN, OPEN + 100, "no")
    assert "trend for the bet" in yes and "24h" in yes and "nudge" in yes and yes.endswith("(shadow, nothing changes)")
    sign = {"yes": yes, "no": no}
    # the spot was above its 24 h value (the template says it fell into the open from ~101): the trend is for a NO bet's way when the spot fell
    import re

    z_yes = float(re.search(r"24h ([+-][0-9.]+)", sign["yes"]).group(1))
    z_no = float(re.search(r"24h ([+-][0-9.]+)", sign["no"]).group(1))
    assert z_yes == pytest.approx(-z_no, abs=0.11) and z_yes != 0
    assert "(shadow" in wavetrend.note(INDEX, OPEN + 99999.0, OPEN + 100000.0, "yes")  # no history for that hour: a sentence, not an error


def test_the_buy_step_only_writes_the_note_and_never_says_no(tmp_path, monkeypatch, mem):  # noqa: F811
    from kalshi_moonshot import lanes

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(TOML.replace('steps = ["price", "wave_top"]', 'steps = ["price", "wave_top", "wave_trend"]'))
    sp = lanes.lane_specs(config.load())["wt"]
    assert sp.buy_chain == ("price", "wave_top", "wave_trend") and "wave_trend" in lanes.BUY_STEPS
    now = OPEN + 100
    for side in ("yes", "no"):
        got = buychain.STEPS["wave_trend"](buychain.Ctx(config.load(), sp, now=now), shot(now, side))
        assert got.ok and "(shadow" in got.why  # no history in memory: still a yes, with the reason
    T, V, _ = background(lead=4000)
    feed(T, V)
    got = buychain.STEPS["wave_trend"](buychain.Ctx(config.load(), sp, now=now), shot(now, "yes"))
    assert got.ok
    assert TICKER  # (the market the helper builds)
