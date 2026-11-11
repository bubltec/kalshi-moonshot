"""The curve exit as a resting ask on a paper lane (lane field curve_rest; watcher._curve_rest_hit): an ask at the curve's
chance plus a margin that shrinks with the time left, repriced every 15 s, sold when the bid reaches it, with the lane's
time-to-go sale as the guard."""

from types import SimpleNamespace

from kalshi_moonshot import curvevalue, digest, manualrest, watcher

CLOSE = 9_900.0


def target(**kw):
    base = dict(ticker="KXBTC15M-1", lane="x", side="yes", curve_margin=-1.0, curve_rest=0.15, rest_price=0.0, rest_at=0.0, curve_at=0.0, close_ts=CLOSE, shock_ref=100.0, shock_index="BRTI",
                curve_behind_last=120.0, curve_behind_chance=0.5)
    base.update(kw)
    return SimpleNamespace(**base)


def run(monkeypatch, t, steps, start=9_300.0):
    """steps: (curve chance, bid) once a second from `start`. Returns (second, reason) of the first sale."""
    monkeypatch.setattr(watcher.ticks_mod.history, "window", lambda *a, **k: ([0.0], [1.0]))
    state = {}
    monkeypatch.setattr(curvevalue, "read", lambda *a, **k: curvevalue.Curve(state["p"], 0.0, 1e-4, 1e-4))
    w = SimpleNamespace(_curve_rest_hit=lambda *a: watcher.ProfitWatcher._curve_rest_hit(None, *a))
    for i, (p, bid) in enumerate(steps):
        state["p"] = p
        now = start + i
        reason = watcher.ProfitWatcher._curve_hit(w, t, bid, now, now)
        if reason:
            return i, reason
    return None, None


def test_the_ask_is_priced_from_the_curve_and_the_bet_sells_when_the_bid_reaches_it(monkeypatch):
    t = target()
    i, reason = run(monkeypatch, t, [(0.50, 0.45)] * 10 + [(0.50, 0.63)] * 3)  # 600 s left: ask 0.50 + 0.15 * sqrt(600/900) = 0.6225 -> 0.63
    assert i == 10 and reason.startswith(manualrest.REST_PREFIX)
    assert manualrest.rest_fill_price(reason) == 0.63


def test_a_bid_under_the_ask_is_held_and_the_ask_follows_the_curve_every_fifteen_seconds(monkeypatch):
    t = target()
    assert run(monkeypatch, t, [(0.50, 0.45)] * 14)[1] is None and t.rest_price == 0.63
    t = target()
    assert run(monkeypatch, t, [(0.50, 0.45)] * 5 + [(0.30, 0.25)] * 9)[1] is None and t.rest_price == 0.63  # not yet repriced
    t = target()
    assert run(monkeypatch, t, [(0.50, 0.45)] * 5 + [(0.30, 0.25)] * 12)[1] is None and t.rest_price == 0.43  # repriced at 15 s


def test_the_guard_sells_at_the_bid_late_when_the_chance_is_under_one_half(monkeypatch):
    i, reason = run(monkeypatch, target(), [(0.30, 0.25)] * 5, start=CLOSE - 100)
    assert i == 0 and reason.startswith("curve exit (time to go)")
    assert run(monkeypatch, target(), [(0.30, 0.25)] * 5, start=CLOSE - 300)[1] is None  # too early for the guard
    assert run(monkeypatch, target(curve_behind_last=-1.0), [(0.30, 0.25)] * 5, start=CLOSE - 100)[1] is None  # no guard set


def test_the_rule_is_off_by_default_and_quiet_in_the_last_seconds(monkeypatch):
    assert run(monkeypatch, target(curve_rest=-1.0), [(0.50, 0.99)] * 20)[1] is None
    assert run(monkeypatch, target(rest_price=0.60), [(0.50, 0.99)] * 3, start=CLOSE - 5)[1] is None


def test_a_resting_fill_is_scored_at_its_own_price_with_no_fee_or_slip():
    row = dict(ask=0.60, side="yes", outcome="yes", exit_price=0.70, exit_reason=manualrest.REST_PREFIX + ": filled at 0.70")
    cost = 0.60 + digest.fee(0.60)
    assert digest._roi(row)[0] == 0.70 / cost - 1
    taker = dict(row, exit_reason="curve exit: bid 0.70")
    assert digest._roi(taker)[0] < digest._roi(row)[0]
