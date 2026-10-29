"""Make a paper bet buy the way the live lane buys.

A live lane that sets buy_rest_seconds rests a post-only bid buy_rest_below under the ask for that long and takes only what fills (runner._place_resting). A lane with entry_approach does the same at its price floor for entry_arm_seconds (runner._place_floor_bid); its paper buys carry FLOOR_MARK so they are judged the same way. A paper bet logged at the decision assumes it filled at the ask, so a paper lane
looks better than the same lane live: it keeps the bets that ran away, which the resting bid misses (commodity over six hours: paper 53 of 54 won, live 25 of 31).

Once a paper bet is older than the rest window, this looks at the tape's quotes and applies the same rule: the bid at (ask - below) fills when the side's ask comes down to it inside the window, and is refused ("post only cross") when the ask
was already at it when the order went in. A filled bet is repriced to the bid; a bet that would not have filled becomes a pass. Nothing here places an order.
"""

from __future__ import annotations

import time
from typing import Any

from .config import Settings
from .lanes import lane_specs
from .store import Store
from .strategy import fee_per_contract

FLOOR_MARK = "[floor-bid"  # runner.evaluate puts this at the front of a paper buy that a live lane would have rested at its floor
MARK = "[rest-v2"
OLD_MARK = "[rest-checked"  # the first version judged by the ask alone; it threw out bets a resting bid does fill, by a trade at its price while the ask stayed higher
GRACE = 5.0  # seconds after the window before a bet is judged, so the last quotes are on the tape


def settle(s: Settings, store: Store, tape: Any, now: float | None = None) -> dict[str, int]:
    """Judge every unjudged paper bet of a resting lane that is older than its rest window. Returns {"filled": n, "missed": n}."""
    now = time.time() if now is None else now
    out = {"filled": 0, "missed": 0}
    specs = {name: sp for name, sp in lane_specs(s).items() if sp.buy_rest_seconds > 0 or sp.entry_approach > 0}
    if not specs or tape is None:
        return out
    for lane, spec in specs.items():
        rows = store.db.execute(
            f"""SELECT id, ts, ticker, side, ask, reason, action, stake_usd FROM decisions
                WHERE lane=? AND mode='observe' AND exit_ts IS NULL AND (action='bet' OR COALESCE(reason,'') LIKE '%{OLD_MARK}%')
                  AND ts < ? AND ts > ? AND COALESCE(reason,'') NOT LIKE '%{MARK}%'""",
            (lane, now - GRACE, now - 24 * 3600),
        ).fetchall()
        for r in rows:
            series = r["ticker"].split("-")[0]
            if s.is_live(lane, series):
                continue  # a live lane's bets were real orders: they filled or they were not bets
            if FLOOR_MARK in (r["reason"] or ""):  # a floor bid: rests at the price logged (the lane's floor) for the arm time
                window, price = spec.entry_arm_seconds, round(float(r["ask"]), 4)
            elif spec.buy_rest_seconds > 0:
                window, price = spec.buy_rest_seconds, round(max(0.01, float(r["ask"]) - spec.buy_rest_below), 4)
            else:
                continue  # a taker buy on a lane that also has a floor bid: it was logged at the ask it took
            if float(r["ts"]) > now - window - GRACE:
                continue  # its window is still open
            quotes = tape.execute(
                "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts > ? AND ts <= ? AND bid > 0 AND ask > 0 ORDER BY ts",
                (r["ticker"], r["ts"] - 10.0, r["ts"] + window),
            ).fetchall()
            if not quotes:
                continue  # no quotes yet: look again later

            def side_ask(q: Any, side: str = r["side"]) -> float:
                return float(q[2]) if side == "yes" else 1.0 - float(q[1])

            at_order = [q for q in quotes if q[0] <= r["ts"]]
            crossed = bool(at_order) and side_ask(at_order[-1]) <= price + 1e-9
            later = [q for q in quotes if q[0] > r["ts"]]
            came = any(side_ask(q) <= price + 1e-9 for q in later)
            if not came:  # a seller can hit our bid while the ask stays above it: a trade by a taker against our side at our price or better
                for fl in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts > ? AND ts <= ?", (r["ticker"], int(r["ts"]), int(r["ts"] + window))).fetchall():
                    if fl[3] is None:
                        continue
                    if r["side"] == "yes" and fl[2] > 0 and fl[3] <= price + 1e-9:  # NO takers sell YES into our bid
                        came = True
                    if r["side"] == "no" and fl[1] > 0 and fl[3] >= 1.0 - price - 1e-9:  # YES takers lift the YES ask we posted
                        came = True
            reason = (r["reason"] or "").split(" " + OLD_MARK)[0].split(" " + MARK)[0][:230]
            if not crossed and came:
                stake = spec.fixed_stake or float(r["stake_usd"] or 0.0)
                if r["action"] == "pass" and stake <= 0:
                    continue  # cannot restore the size of a bet the first version threw out
                n = round(stake / (price + fee_per_contract(price)), 2) if r["action"] == "pass" else None
                store.db.execute(
                    "UPDATE decisions SET action='bet', ask=?, contracts=COALESCE(?, contracts), stake_usd=COALESCE(?, stake_usd), reason=? WHERE id=?",
                    (price, n, round(stake, 4) if n is not None else None, f"{reason} {MARK}: a resting bid at {price:.2f} would have filled]", r["id"]),
                )
                out["filled"] += 1
            else:
                why = "the ask was already at our bid (post only cross)" if crossed else "nothing traded at our bid and the ask never came down to it"
                store.db.execute(
                    "UPDATE decisions SET action='pass', contracts=NULL, stake_usd=NULL, outcome=NULL, reason=? WHERE id=?",
                    (f"{reason} {MARK}: a resting bid at {price:.2f} would not have filled, {why}]", r["id"]),
                )
                out["missed"] += 1
        store.db.commit()
    return out
