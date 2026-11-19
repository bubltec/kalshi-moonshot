from kalshi_moonshot import config, watcher


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


def test_giveback_sells_when_the_gain_is_nearly_gone():
    series = [*PEAK_THEN_DIP[:-2], (1011.0, 0.79), (1060.0, 0.79)]  # 0.11 of the 0.12 gain given back, for 49 s
    w = feed(target(0.90), series)
    w.check_timers(1070.0)
    assert not w._jobs.empty()
    assert w._jobs.get_nowait()[2].startswith("peak-crash")

