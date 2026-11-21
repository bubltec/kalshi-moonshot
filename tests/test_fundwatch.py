"""The exchange funds watch (fundwatch.py, user 2026-11-21): every shard a live lane trades on is kept at the top-up target in the background, and a shard that cannot be funded is reported."""
import logging
from types import SimpleNamespace

from kalshi_moonshot import config, fundwatch, runner
from kalshi_moonshot.store import Store


class Client:
    def __init__(self, cash, shard_of=None):
        self.cash = dict(cash)
        self.moves = []
        self.authed = True
        self.shard_of = shard_of or {"KXGOLDD": 0, "KXSILVERD": 1}
        self.market_calls = 0

    def series_markets(self, series):
        return [{"ticker": f"{series}-1"}]

    def market(self, ticker):
        self.market_calls += 1
        return {"exchange_index": self.shard_of[ticker.split("-")[0]]}

    def shard_balances(self):
        return dict(self.cash)

    def shard_transfer(self, usd, source, src, destination, dst):
        self.moves.append((usd, src, dst))
        self.cash[src] -= usd
        self.cash[dst] += usd
        return {"transfer_id": "t"}


def make(tmp_path, monkeypatch, live="gold,silver", **env):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("LIVE_LANES", live)
    monkeypatch.setenv("SHARD_TOPUP_TARGET_USD", "10")
    monkeypatch.setenv("SHARD_TOPUP_KEEP_USD", "10")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    runner._EXCHANGE_OF.clear()
    runner._UNFUNDED.clear()
    fundwatch._SHARD_OF.clear()
    fundwatch._LAST_ALERT.clear()
    monkeypatch.setattr(runner.time, "sleep", lambda x: None)
    s = config.load()
    specs = {"gold": SimpleNamespace(series=("KXGOLDD",)), "silver": SimpleNamespace(series=("KXSILVERD",)), "paperlane": SimpleNamespace(series=("KXOILD",))}
    return s, Store(s.db_path), specs


def test_a_live_shard_under_its_target_is_topped_up_in_the_background(tmp_path, monkeypatch):
    s, store, specs = make(tmp_path, monkeypatch)
    c = Client({0: 2.0, 1: 40.0, 2: 100.0})
    assert fundwatch.check_once(s, c, store, specs) == []
    assert c.moves == [(8.0, 2, 0)] or c.moves == [(10.0, 2, 0)]  # from the richest other exchange; the exact amount is the existing top-up rule's
    assert c.cash[0] >= 10.0 and c.cash[1] == 40.0


def test_a_shard_that_cannot_be_funded_is_a_problem_naming_the_exchange_and_the_series(tmp_path, monkeypatch):
    s, store, specs = make(tmp_path, monkeypatch)
    c = Client({0: 2.0, 1: 40.0})  # no other exchange has cash above the keep floor except 1, which can move $30
    c.cash[1] = 10.0  # at the floor: nothing to move
    problems = fundwatch.check_once(s, c, store, specs)
    assert problems and "exchange 0" in problems[0] and "KXGOLDD" in problems[0] and "$2.00" in problems[0]
    assert c.moves == []


def test_the_account_total_that_cannot_cover_every_live_shard_is_a_problem(tmp_path, monkeypatch):
    s, store, specs = make(tmp_path, monkeypatch)
    c = Client({0: 5.0, 1: 4.0})
    problems = fundwatch.check_once(s, c, store, specs)
    assert any("holds $9.00 in all" in p and "2 live exchange" in p for p in problems)


def test_a_lane_that_is_not_live_is_not_watched(tmp_path, monkeypatch):
    s, store, specs = make(tmp_path, monkeypatch, live="gold")
    c = Client({0: 50.0, 1: 0.0})  # silver's shard is empty but silver is paper
    assert fundwatch.check_once(s, c, store, specs) == [] and c.moves == []


def test_it_does_nothing_outside_trade_mode_or_with_the_top_up_off(tmp_path, monkeypatch):
    s, store, specs = make(tmp_path, monkeypatch, MODE="observe")
    assert fundwatch.check_once(s, Client({0: 0.0}), store, specs) == []
    s, store, specs = make(tmp_path, monkeypatch, SHARD_TOPUP_TARGET_USD="0")
    assert fundwatch.check_once(s, Client({0: 0.0}), store, specs) == []


def test_the_shard_of_a_series_is_asked_once(tmp_path, monkeypatch):
    s, store, specs = make(tmp_path, monkeypatch)
    c = Client({0: 50.0, 1: 50.0})
    fundwatch.check_once(s, c, store, specs)
    fundwatch.check_once(s, c, store, specs)
    assert c.market_calls == 2  # one for each series, not one for each pass


def test_a_problem_is_announced_at_most_once_an_hour_even_if_it_clears_and_returns(tmp_path, caplog):
    fundwatch._LAST_ALERT.clear()
    sent = []
    with caplog.at_level(logging.WARNING, logger="moonshot"):
        assert fundwatch.report(["a"], now=1000.0, put_metric=sent.append) == ["a"]
        assert fundwatch.report(["a"], now=1100.0, put_metric=sent.append) == []  # still there: not announced again
        assert fundwatch.report([], now=1200.0, put_metric=sent.append) == []  # cleared
        assert fundwatch.report(["a"], now=1300.0, put_metric=sent.append) == []  # back within the hour: no second email
        assert fundwatch.report(["a"], now=4700.0, put_metric=sent.append) == ["a"]  # an hour later and still short: again
    assert sent == [1.0, 0.0, 0.0, 0.0, 1.0]  # the alarm emails when the metric reaches 1, so these are the only two emails
    assert caplog.text.count("FUNDS ALERT: a") == 2
