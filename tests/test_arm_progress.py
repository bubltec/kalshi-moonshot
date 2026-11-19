from kalshi_moonshot import config, watcher


def target(entry, progress, **kw):
    t = watcher.Target("KXPALLADIUM15M-1", "fav", "no", entry, 0.0, 0.0, 0.0, 30.0, 0.10, 0.10, 0.04, 10.0, 0.12, 3.0, entry)
    t.trail_progress = progress
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def feed(t, series):
    w = watcher.ProfitWatcher(lambda: config.load(), None, lambda: None)
    w.targets[("KXPALLADIUM15M-1", "fav")] = t
    for ts, bid in series:
        w.on_event({"kind": "quote", "ticker": "KXPALLADIUM15M-1", "bid": 1 - bid - 0.01, "ask": 1 - bid, "ts": ts})  # a NO position: its bid is 1 - yes ask
    return w


def rise_then_fall(peak, low):
    return [(1000.0, 0.50), (1001.0, peak), (1005.0, peak), (1006.0, peak), (1010.0, peak), (1011.0, low), (1040.0, low)]


def test_it_arms_at_the_share_of_the_distance_to_the_payout():
    assert round(watcher.arm_gain(target(0.45, 0.40)), 4) == round((0.45 + 0.40 * 0.55) / 0.45 - 1, 4)  # arms at 0.67
    assert round(0.45 * (1 + watcher.arm_gain(target(0.45, 0.40))), 2) == 0.67
    assert round(0.63 * (1 + watcher.arm_gain(target(0.63, 0.40))), 3) == 0.778
    assert round(0.85 * (1 + watcher.arm_gain(target(0.85, 0.40))), 3) == 0.91


def test_a_dip_after_it_arms_sells_when_the_gain_is_given_back():
    w = feed(target(0.45, 0.40), rise_then_fall(0.80, 0.46))  # armed at 0.67, 0.34 of 0.35 gain given back
    w.check_timers(1050.0)
    assert not w._jobs.empty()
    assert w._jobs.get_nowait()[2].startswith("peak-crash")

