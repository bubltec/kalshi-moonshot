import pytest

from tests.test_settlement import _watcher


def _held(tmp_path, monkeypatch, drop=0.25, ticker="KXCOPPER15M-1"):
    w, _s, _store, close = _watcher(tmp_path, monkeypatch, ticker=ticker)
    (tmp_path / "strategies.toml").write_text(
        f"[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nsettlement_flip=true\nseries=['{ticker.split('-')[0]}']\nalways=true\nstop_loss=false\ndrop_cents={drop}\n"
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
    (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nsettlement_flip=true\nseries=['KXCOPPER15M']\nalways=true\nstop_loss=false\ncash_left_seconds=5\n")
    w.refresh()  # watched for another reason, drop_cents left at 0
    for k in range(40, 5, -1):
        _quote(w, "KXCOPPER15M-1", 0.85, close - 100 - k)
    _fall(w, "KXCOPPER15M-1", close - 100, [0.85, 0.82, 0.74, 0.55])
    assert w._jobs.empty()
