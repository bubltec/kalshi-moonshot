from kalshi_moonshot import config, watcher


def target(**kw):
    t = watcher.Target("KXBTC15M-1", "fav", "yes", 0.60, 0.0, 0.0, 0.0, 30.0, 0.10, 0.0, 0.0, 10.0, 0.0, 3.0, 0.60)
    t.hover_dip = 0.50
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def feed(t, series):
    w = watcher.ProfitWatcher(lambda: config.load(), None, lambda: None)
    w.targets[("KXBTC15M-1", "fav")] = t
    for ts, bid in series:
        w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": bid, "ask": bid + 0.01, "ts": ts})
    return w


def test_it_does_not_sell_a_profit_that_never_dipped():
    w = feed(target(), [(1000.0, 0.62), (1010.0, 0.66), (1020.0, 0.70)])
    assert w._jobs.empty()


def test_it_does_not_sell_while_still_under_the_cost_after_dipping():
    w = feed(target(), [(1000.0, 0.48), (1010.0, 0.52), (1020.0, 0.60)])
    assert w._jobs.empty()

