from kalshi_moonshot import config, watcher


def target(giveback):
    t = watcher.Target("KXBTC15M-1", "fav", "yes", 0.80, 0.0, 0.0, 0.0, 30.0, 0.10, 0.10, 0.04, 10.0, 0.12, 3.0, 0.80)
    t.trail_giveback = giveback
    t.trail_fast = 0.20
    return t


def feed(t, series):
    w = watcher.ProfitWatcher(lambda: config.load(), None, lambda: None)
    w.targets[("KXBTC15M-1", "fav")] = t
    for ts, bid in series:
        w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": bid, "ask": bid + 0.01, "ts": ts})
    return w


# paid 0.80, peak 0.96 (held), then a 24c fall to 0.72: under the price paid
GAP = [(1000.0, 0.82), (1001.0, 0.96), (1005.0, 0.96), (1006.0, 0.96), (1010.0, 0.96), (1011.0, 0.72), (1060.0, 0.72)]


def test_the_crash_exit_does_not_sell_under_the_price_paid():
    w = feed(target(0.90), GAP)
    w.check_timers(1070.0)
    assert w._jobs.empty()  # the stop-loss owns the bet from here


def test_it_still_sells_a_fall_that_stays_above_the_price_paid():
    series = [*GAP[:-2], (1011.0, 0.84), (1060.0, 0.84)]  # 0.16 gain, 0.12 given back (75%): under the 80% line
    w = feed(target(0.80), series)  # a dip short of the give-back line: nothing
    w.check_timers(1070.0)
    assert w._jobs.empty()
    series2 = [*GAP[:-2], (1011.0, 0.81), (1060.0, 0.81)]  # 0.15 of 0.16 given back, still not under 0.80
    w2 = feed(target(0.90), series2)
    w2.check_timers(1070.0)
    assert not w2._jobs.empty() and w2._jobs.get_nowait()[2].startswith("peak-crash")


def test_the_plain_trailing_exit_keeps_its_old_behavior():
    w = feed(target(0.0), GAP)  # give-back off: the fast drop (0.20) fires under the price paid, as before
    assert not w._jobs.empty()
