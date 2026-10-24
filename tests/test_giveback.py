from kalshi_moonshot import config, lanes, watcher


def target(giveback):
    t = watcher.Target("KXBTC15M-1", "fav", "yes", 0.78, 0.0, 0.0, 0.0, 30.0, 0.10, 0.10, 0.04, 10.0, 0.12, 3.0, 0.78)
    t.trail_giveback = giveback
    return t


def feed(t, series):
    w = watcher.ProfitWatcher(lambda: config.load(), None, lambda: None)
    w.targets[("KXBTC15M-1", "fav")] = t
    for ts, bid in series:
        w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": bid, "ask": bid + 0.01, "ts": ts})
    return w


PEAK_THEN_DIP = [(1000.0, 0.80), (1001.0, 0.90), (1005.0, 0.90), (1006.0, 0.90), (1010.0, 0.90), (1011.0, 0.84), (1060.0, 0.84)]  # bought 0.78, peak 0.90, back to 0.84: half the gain


def test_the_fixed_drop_sells_a_dip_while_still_well_in_profit():
    w = feed(target(0.0), PEAK_THEN_DIP)
    w.check_timers(1070.0)
    assert not w._jobs.empty()


def test_giveback_keeps_the_position_until_most_of_the_gain_is_gone():
    w = feed(target(0.90), PEAK_THEN_DIP)
    w.check_timers(1070.0)
    assert w._jobs.empty()  # 0.06 of a 0.12 gain given back


def test_giveback_sells_when_the_gain_is_nearly_gone():
    series = [*PEAK_THEN_DIP[:-2], (1011.0, 0.79), (1060.0, 0.79)]  # 0.11 of the 0.12 gain given back, for 49 s
    w = feed(target(0.90), series)
    w.check_timers(1070.0)
    assert not w._jobs.empty()
    assert w._jobs.get_nowait()[2].startswith("peak-crash")


def test_the_drop_and_the_fast_exit_follow_the_gain():
    t = target(0.90)
    t.peak = 0.90
    assert round(watcher.crash_drop(t), 3) == 0.108
    assert watcher.crash_fast(t) == 0.12  # the fixed gap-down distance is the floor when it is larger
    t.peak = 0.82
    assert round(watcher.crash_drop(t), 3) == 0.036 and watcher.crash_fast(t) == 0.12
    t.trail_giveback = 0.0
    assert watcher.crash_drop(t) == 0.04 and watcher.crash_fast(t) == 0.12


def test_the_default_is_90_percent_and_the_lane_follows_it(monkeypatch):
    monkeypatch.delenv("CRASH_GIVEBACK")
    monkeypatch.delenv("CRASH_SPOT_SIGMA")
    s = config.load()
    assert s.crash_giveback == 0.90
    spec = lanes.lane_specs(s)["commodity"]
    assert spec.trail_giveback is None
    assert lanes.settings_of(spec, s)["crash_giveback"] == "0.9"
