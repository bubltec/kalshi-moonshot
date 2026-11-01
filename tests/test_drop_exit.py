import pytest

from tests.test_settlement import _watcher


def _held(tmp_path, monkeypatch, drop=0.25, ticker="KXCOPPER15M-1", extra=""):
    w, _s, _store, close = _watcher(tmp_path, monkeypatch, ticker=ticker)
    (tmp_path / "strategies.toml").write_text(
        f"[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nburst_exit=true\nseries=['{ticker.split('-')[0]}']\nalways=true\nstop_loss=false\ndrop_cents={drop}\n{extra}"
    )
    w.refresh()
    t = w.targets[(ticker, "guarded")]
    t.stop_drop = 0.0  # the fixture's first refresh armed the default stop on this target; these tests are about the drop exit alone
    return w, t, close


def _quote(w, ticker, yes_bid, ts):
    w.on_event({"kind": "quote", "ticker": ticker, "bid": yes_bid, "ask": round(yes_bid + 0.01, 2), "ts": ts})


def _fall(w, ticker, now, bids):
    """Quotes one second apart ending at `now`; a YES holder, so the quote's bid is the held side's bid."""
    for k, b in enumerate(bids):
        _quote(w, ticker, b, now - (len(bids) - 1 - k))


def test_the_lane_setting_reaches_the_held_market(tmp_path, monkeypatch):
    _w, t, _close = _held(tmp_path, monkeypatch)
    assert t.drop_cents == pytest.approx(0.25) and t.drop_seconds == 3.0 and t.drop_was == pytest.approx(0.76)


def test_a_big_fast_fall_in_the_last_two_minutes_sells(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch)
    for k in range(40, 5, -1):
        _quote(w, "KXCOPPER15M-1", 0.85, close - 100 - k)
    _fall(w, "KXCOPPER15M-1", close - 100, [0.85, 0.82, 0.74, 0.55])  # the copper shape: 0.85 to 0.55 in three seconds
    assert not w._jobs.empty()
    assert w._jobs.get_nowait()[2].startswith("drop: bid 0.55 fell 0.30")


def test_a_small_fall_is_not_a_drop(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch)
    for k in range(40, 5, -1):
        _quote(w, "KXCOPPER15M-1", 0.85, close - 100 - k)
    _fall(w, "KXCOPPER15M-1", close - 100, [0.85, 0.80, 0.74, 0.70])  # 15c in three seconds is under the 25c bar
    assert w._jobs.empty()


def test_a_fall_before_the_last_two_minutes_is_left_alone(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch)
    for k in range(40, 5, -1):
        _quote(w, "KXCOPPER15M-1", 0.85, close - 300 - k)
    _fall(w, "KXCOPPER15M-1", close - 300, [0.85, 0.82, 0.74, 0.55])  # five minutes out the market has time to come back
    assert w._jobs.empty()


def test_a_long_shot_sliding_is_not_a_favorite_falling(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch)
    for k in range(70, 5, -1):
        _quote(w, "KXCOPPER15M-1", 0.50, close - 100 - k)  # never 0.76 in the last minute
    _fall(w, "KXCOPPER15M-1", close - 100, [0.50, 0.45, 0.35, 0.20])
    assert w._jobs.empty()


def test_the_exit_is_off_unless_the_lane_sets_it(tmp_path, monkeypatch):
    w, _s, _store, close = _watcher(tmp_path, monkeypatch, ticker="KXCOPPER15M-1")
    (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nburst_exit=true\nseries=['KXCOPPER15M']\nalways=true\nstop_loss=false\ncash_left_seconds=5\n")
    w.refresh()  # watched for another reason, drop_cents left at 0
    for k in range(40, 5, -1):
        _quote(w, "KXCOPPER15M-1", 0.85, close - 100 - k)
    _fall(w, "KXCOPPER15M-1", close - 100, [0.85, 0.82, 0.74, 0.55])
    assert w._jobs.empty()


def _settle_quiet(w, close, left, bid=0.85):
    for k in range(40, 5, -1):
        _quote(w, "KXCOPPER15M-1", bid, close - left - k)


def test_a_confirmed_fall_that_stays_down_sells_after_the_wait(tmp_path, monkeypatch):
    w, t, close = _held(tmp_path, monkeypatch, drop=0.15, extra="drop_seconds=5\ndrop_confirm=4\ndrop_stay=0.10\n")
    _settle_quiet(w, close, 100)
    _fall(w, "KXCOPPER15M-1", close - 100, [0.85, 0.80, 0.74, 0.60])
    assert w._jobs.empty() and t.drop_armed_at is not None  # the fall only arms the exit
    for k in (1, 2, 3):
        _quote(w, "KXCOPPER15M-1", 0.58, close - 100 + k)
    assert w._jobs.empty()
    _quote(w, "KXCOPPER15M-1", 0.57, close - 100 + 4)  # four seconds on, still 28c under the high
    assert w._jobs.get_nowait()[2].startswith("drop: bid 0.57 is still 0.28 under 0.85")


def test_a_fall_that_bounces_back_inside_the_wait_is_a_spike_and_is_held(tmp_path, monkeypatch):
    w, t, close = _held(tmp_path, monkeypatch, drop=0.15, extra="drop_seconds=5\ndrop_confirm=4\ndrop_stay=0.10\n")
    _settle_quiet(w, close, 100)
    _fall(w, "KXCOPPER15M-1", close - 100, [0.85, 0.80, 0.74, 0.60])
    for k, b in ((1, 0.70), (2, 0.78), (3, 0.82), (4, 0.84)):
        _quote(w, "KXCOPPER15M-1", b, close - 100 + k)
    assert w._jobs.empty() and t.drop_armed_at is None


def test_the_window_is_the_lanes_own(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch, extra="drop_window=180\n")
    _settle_quiet(w, close, 150)
    _fall(w, "KXCOPPER15M-1", close - 150, [0.85, 0.82, 0.74, 0.55])
    assert not w._jobs.empty()  # 150 s left is inside a 180 s window
    (tmp_path / "b").mkdir()
    w2, _t2, close2 = _held(tmp_path / "b", monkeypatch)
    _settle_quiet(w2, close2, 150)
    _fall(w2, "KXCOPPER15M-1", close2 - 150, [0.85, 0.82, 0.74, 0.55])
    assert w2._jobs.empty()  # and outside the default 120 s one


def test_the_other_side_is_bought_when_the_fall_is_found_before_the_sale(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch, drop=0.15, extra="drop_seconds=5\ndrop_confirm=4\ndrop_stay=0.10\ndrop_buy=true\ndrop_buy_max=0.40\nburst_buy=1.0\n")
    _settle_quiet(w, close, 100)
    _fall(w, "KXCOPPER15M-1", close - 100, [0.85, 0.80, 0.74, 0.68])  # the YES bid is 0.68, so NO costs 0.32
    assert w._jobs.empty()  # not sold yet: the fall is only armed
    sig = w._buys.get_nowait()
    assert sig.kind == "flip" and sig.side == "no" and sig.ask == pytest.approx(0.32)
    for k in (1, 2, 3, 4):
        _quote(w, "KXCOPPER15M-1", 0.60, close - 100 + k)
    assert w._jobs.get_nowait()[2].startswith("drop:")  # the sale comes after, once confirmed
    assert w._buys.empty()  # and only one other-side buy per market


def test_the_other_side_is_not_bought_when_it_costs_more_than_the_lane_allows(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch, drop=0.15, extra="drop_confirm=4\ndrop_stay=0.10\ndrop_buy=true\ndrop_buy_max=0.25\nburst_buy=1.0\n")
    _settle_quiet(w, close, 100)
    _fall(w, "KXCOPPER15M-1", close - 100, [0.85, 0.80, 0.74, 0.68])  # NO costs 0.32, over the 0.25 cap
    assert w._buys.empty()


def test_a_fall_that_bounces_back_has_still_bought_the_hedge_once(tmp_path, monkeypatch):
    w, _t, close = _held(tmp_path, monkeypatch, drop=0.15, extra="drop_confirm=4\ndrop_stay=0.10\ndrop_buy=true\ndrop_buy_max=0.40\nburst_buy=1.0\n")
    _settle_quiet(w, close, 100)
    _fall(w, "KXCOPPER15M-1", close - 100, [0.85, 0.80, 0.74, 0.68])
    for k, b in ((1, 0.74), (2, 0.80), (3, 0.83), (4, 0.84)):
        _quote(w, "KXCOPPER15M-1", b, close - 100 + k)
    assert w._jobs.empty() and not w._buys.empty()  # held through the spike; the small hedge was already bought
