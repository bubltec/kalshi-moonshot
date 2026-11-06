from types import SimpleNamespace

from kalshi_moonshot import lanehealth


def rows(lane, n, action="pass", reason="the index history is too short to read the trend before the last 90 s. [x] 0.91"):
    return [{"lane": lane, "action": action, "reason": reason} for _ in range(n)]


def test_a_lane_skipped_for_one_reason_every_time_is_silent():
    got = lanehealth.silent_lanes(rows("treasury_favorite", 30), {"treasury_favorite"})
    assert [g["lane"] for g in got] == ["treasury_favorite"] and got[0]["share"] == 1.0


def test_a_lane_that_bet_or_has_few_evaluations_or_mixed_reasons_is_not():
    live = {"a", "b", "c"}
    data = rows("a", 30) + rows("a", 1, action="bet", reason="bought") + rows("b", 5)
    data += rows("c", 15, reason="one reason") + rows("c", 15, reason="another reason")
    assert lanehealth.silent_lanes(data, live) == []


def test_a_lane_that_is_not_live_is_ignored():
    assert lanehealth.silent_lanes(rows("paper_lane", 30), {"treasury_favorite"}) == []


def test_numbers_in_a_reason_do_not_split_it():
    data = [{"lane": "a", "action": "pass", "reason": f"[z +{i / 10:.2f}] crossed {i}c {i} times"} for i in range(30)]
    assert len(lanehealth.silent_lanes(data, {"a"})) == 1


def test_a_bet_unsettled_two_hours_after_the_close_is_stale():
    now = 1_800_000_000.0
    closed = "2026-11-05T08:00:00Z"
    from datetime import datetime

    t = datetime.fromisoformat("2026-11-05T08:00:00+00:00").timestamp()
    got = lanehealth.stale_bets([{"lane": "treasury_favorite", "ticker": "KX2YRRATE15M-26NOV050300-00", "close_time": closed}], t + 3 * 3600)
    assert got and got[0]["hours"] == 3.0
    assert lanehealth.stale_bets([{"lane": "x", "ticker": "T", "close_time": closed}], t + 1800) == []
    assert lanehealth.stale_bets([{"lane": "x", "ticker": "T", "close_time": "garbage"}], now) == []


def test_check_writes_its_report(tmp_path):
    lanehealth._checked_at = 0.0
    lanehealth._reported.clear()
    db = SimpleNamespace(execute=lambda sql, args=(): SimpleNamespace(fetchall=lambda: rows("treasury_favorite", 30) if "action, reason" in sql else []))
    found = lanehealth.check(SimpleNamespace(data_dir=str(tmp_path)), SimpleNamespace(db=db), {"treasury_favorite"}, now=1_800_000_000.0)
    assert found and (tmp_path / "lane_health.json").exists()
    assert lanehealth.check(SimpleNamespace(data_dir=str(tmp_path)), SimpleNamespace(db=db), {"treasury_favorite"}, now=1_800_000_100.0) == []  # once an hour
