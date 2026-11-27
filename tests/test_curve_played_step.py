"""The priskp buy step refuses a swing the lane already bought, from the lane's OWN bet rows (buychain.priskp_step; Store.lane_bet_times). 2026-11-27, user: "why doesn't the rebuy follow priskp logic, which should have done none of this and waited for the next curve"; "lanes only manage their own bets"."""
from kalshi_moonshot import buychain, config, priskp
from tests.test_cooldown_step import NOW, TICKER, shot, spec, store_with


def ask(st, lane="x"):
    import dataclasses

    sh = dataclasses.replace(shot("yes"), lane=lane)  # the shot is frozen
    return buychain.STEPS["priskp"](buychain.Ctx(config.load(), spec(), p_yes=0.9, store=st, now=NOW), sh)


def test_the_step_waits_for_the_next_swing_after_the_lanes_own_bet_in_this_one(tmp_path, monkeypatch):
    st = store_with(tmp_path, monkeypatch, [("yes", 0.67, 0.80, 10.0)])  # lane x bought YES of this market and sold it ten seconds ago
    monkeypatch.setattr(priskp, "admit", lambda *a, **k: (True, "the curve says buy"))
    monkeypatch.setattr(priskp, "swing_played", lambda shot, now, hours, times: bool(times))  # the reading itself is tested in test_priskp_step
    got = ask(st)
    assert got.ok is False and "curve_played" in got.why
    assert ask(st, lane="y").ok is True  # another lane's bets are not this lane's: it has not bought this swing
    st2 = store_with(tmp_path / "none", monkeypatch, [])
    assert ask(st2).ok is True  # no bet of the lane here yet
    monkeypatch.setattr(priskp, "admit", lambda *a, **k: (False, "the curve says wait"))
    assert ask(st).ok is False and "curve_played" not in ask(st).why  # a wait from the curve itself is passed on as it was
