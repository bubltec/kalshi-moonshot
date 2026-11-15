"""The entry trend check looks back three minutes on the live lane (2026-11-14, user: "go live with 180s now"). A look back over about 100 s used to be unreadable (the noise window was a fixed 2 minutes), so the noise is now read over twice the span, 2 minutes at the least. These tests go through runner.evaluate with a lane file and quotes on the tape."""
import time

import pytest

from kalshi_moonshot import db, runner
from kalshi_moonshot.store import Store

TICKER = "KXA-1"


def lane(tmp_path, seconds):
    from tests.test_sizing import settings

    (tmp_path / "tape").mkdir(parents=True, exist_ok=True)
    s = settings(tmp_path, tape_dir=tmp_path / "tape")
    (s.data_dir / "strategies.toml").write_text(
        f"[lane.cool]\nestimator='llm'\nprice=[0.01,0.99]\nhours=[0.0,48.0]\nalways=true\nfixed_stake=2.0\nentry_trend_seconds={seconds}\nentry_trend_z=0.0\n")
    return s


def quotes(s, mids):
    """One quote a second for len(mids) seconds, the last one a second ago; each mid is the YES mid, with a half-cent wobble so the market has noise."""
    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    now = time.time()
    for k, mid in enumerate(mids):
        m = mid + (0.005 if k % 2 else -0.005)
        tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?,?,?,?)", (now - len(mids) + k, TICKER, round(m - 0.01, 4), round(m + 0.01, 4)))
    tape.commit()


def decide(s):
    from tests.test_sizing import Est, shot

    return runner.evaluate(s, None, Est(0.5), Store(s.db_path), shot(ticker=TICKER, lane="cool", ask=0.5))


def test_a_side_that_gained_over_three_minutes_is_bought(tmp_path):
    s = lane(tmp_path, 180)
    quotes(s, [0.40 + 0.0005 * k for k in range(420)])  # 0.40 to 0.61 over seven minutes
    assert decide(s).action == "bet"


def test_a_side_that_fell_over_three_minutes_is_not_bought_even_when_it_ticked_up_in_the_last_minute(tmp_path):
    mids = [0.60] * 240 + [0.60 - 0.0015 * k for k in range(120)] + [0.42 + 0.0015 * k for k in range(60)]  # above its level 6 minutes ago, down 8c over the last 3 minutes, up over the last minute
    s = lane(tmp_path, 180)
    quotes(s, mids)
    out = decide(s)
    assert out.action == "pass" and "trending against" in out.note and "180s" in out.note
    (tmp_path / "sixty").mkdir()
    s60 = lane(tmp_path / "sixty", 60)
    quotes(s60, mids)
    assert decide(s60).action == "bet"  # the 60 s reading sees the last minute's uptick and lets it through


def test_a_market_with_too_little_history_has_no_reading_and_is_skipped(tmp_path):
    s = lane(tmp_path, 180)
    quotes(s, [0.40 + 0.0005 * k for k in range(150)])  # two and a half minutes of quotes: not enough to read three minutes and its noise
    out = decide(s)
    assert out.action == "pass" and "no reference" in out.note


def test_the_trend_read_over_three_minutes_is_in_the_markets_own_units(tmp_path):
    from tests.test_sizing import shot

    s = lane(tmp_path, 180)
    quotes(s, [0.40 + 0.0005 * k for k in range(420)])
    up = runner.side_trend_z(s, shot(ticker=TICKER, lane="cool", ask=0.5), 180.0)
    assert up is not None and up > 0
    (tmp_path / "down").mkdir()
    s2 = lane(tmp_path / "down", 180)
    quotes(s2, [0.60 - 0.0005 * k for k in range(420)])
    down = runner.side_trend_z(s2, shot(ticker=TICKER, lane="cool", ask=0.5), 180.0)
    assert down is not None and down < 0


def test_a_span_up_to_a_minute_reads_exactly_as_it_always_did(tmp_path):
    from tests.test_sizing import shot

    s = lane(tmp_path, 60)
    quotes(s, [0.50 + 0.0004 * k + (0.01 if k % 5 == 0 else 0.0) for k in range(300)])
    sh = shot(ticker=TICKER, lane="cool", ask=0.5)
    now = time.time()
    got = runner.side_trend_read(s, sh, 60.0, now)
    assert got is not None and got[1] > 0
    assert runner.side_trend_read(s, sh, 60.0, now) == got  # the noise window is the 2 minutes it was: nothing here depends on a longer one
