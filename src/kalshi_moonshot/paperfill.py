"""Make a paper bet buy the way the live lane buys.

A live lane that sets buy_rest_seconds rests a post-only bid buy_rest_below under the ask for that long and takes only what fills (runner._place_resting). A lane with entry_approach does the same at its price floor for entry_arm_seconds (runner._place_floor_bid); its paper buys carry FLOOR_MARK so they are judged the same way. A paper bet logged at the decision assumes it filled at the ask, so a paper lane
looks better than the same lane live: it keeps the bets that ran away, which the resting bid misses (commodity over six hours: paper 53 of 54 won, live 25 of 31).

Once a paper bet is older than the rest window, this looks at the tape's quotes and applies the same rule: the bid at (ask - below) fills when the side's ask comes down to it inside the window, and is refused ("post only cross") when the ask
was already at it when the order went in. A filled bet is repriced to the bid; a bet that would not have filled becomes a pass. Nothing here places an order.
"""

from __future__ import annotations

import time
from typing import Any

from . import restprice
from .config import Settings
from .fillmodel import simulate_taker_buy
from .lanes import lane_specs
from .store import Store
from .strategy import fee_per_contract

MARK = "[rest-v2"
FOLLOW_UP = 8.0  # seconds after the rest window in which the live lane's fallback buy lands (the order, its retry and the fill)
MARK_FM = "[fill-model"  # a taker paper bet judged on the tape (settle_taker)
OLD_MARK = "[rest-checked"  # the first version judged by the ask alone; it threw out bets a resting bid does fill, by a trade at its price while the ask stayed higher
GRACE = 5.0  # seconds after the window before a bet is judged, so the last quotes are on the tape


def settle(s: Settings, store: Store, tape: Any, now: float | None = None) -> dict[str, int]:
    """Judge every unjudged paper bet of a resting lane that is older than its rest window. Returns {"filled": n, "missed": n}."""
    now = time.time() if now is None else now
    out = {"filled": 0, "missed": 0}
    specs = {name: sp for name, sp in lane_specs(s).items() if sp.buy_rest_seconds > 0}
    if not specs or tape is None:
        return out
    for lane, spec in specs.items():
        rows = store.db.execute(
            f"""SELECT id, ts, ticker, side, ask, reason, action, stake_usd FROM decisions
                WHERE lane=? AND mode='observe' AND exit_ts IS NULL AND ((action='bet' AND stake_usd IS NOT NULL) OR COALESCE(reason,'') LIKE '%{OLD_MARK}%')
                  AND ts < ? AND ts > ? AND COALESCE(reason,'') NOT LIKE '%{MARK}%' ORDER BY ts""",
            (lane, now - GRACE, now - 24 * 3600),
        ).fetchall()
        for r in rows:
            series = r["ticker"].split("-")[0]
            if s.is_live(lane, series):
                continue  # a live lane's bets were real orders: they filled or they were not bets
            held = store.db.execute(  # an earlier resting bid of this lane on this market that filled and is still open: this one would not have been placed (the live lane sees its position and skips)
                f"""SELECT 1 FROM decisions WHERE lane=? AND ticker=? AND id<>? AND ts < ? AND action='bet' AND stake_usd IS NOT NULL
                    AND (COALESCE(reason,'') LIKE '%{MARK}: a resting bid at%would have filled%' OR COALESCE(reason,'') LIKE '%{MARK}: the resting bid at%then takes the ask%') AND (exit_ts IS NULL OR exit_ts > ?) LIMIT 1""",
                (lane, r["ticker"], r["id"], r["ts"], r["ts"]),
            ).fetchone()
            if held is not None and r["action"] == "bet":
                reason = (r["reason"] or "").split(" " + MARK)[0]
                store.db.execute(
                    "UPDATE decisions SET action='pass', contracts=NULL, stake_usd=NULL, outcome=NULL, reason=? WHERE id=?",
                    (f"{reason} {MARK}: already holding this market (paper), an earlier resting bid of this lane filled]", r["id"]),
                )
                out["missed"] += 1
                continue
            if spec.buy_rest_seconds > 0:
                window, price = spec.buy_rest_seconds, round(max(0.01, float(r["ask"]) - spec.buy_rest_below), 4)  # (the average-ask start, if the lane sets one, replaces this once the quotes are read, below)
            else:
                continue  # a taker buy on a lane that also has a floor bid: it was logged at the ask it took
            if float(r["ts"]) > now - window - GRACE:
                continue  # its window is still open
            quotes = tape.execute(
                "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts > ? AND ts <= ? AND bid > 0 AND ask > 0 ORDER BY ts",
                (r["ticker"], r["ts"] - max(10.0, spec.buy_rest_avg_seconds), r["ts"] + window + FOLLOW_UP),
            ).fetchall()
            if not quotes:
                continue  # no quotes yet: look again later
            if spec.buy_rest_avg_seconds > 0:  # the order starts under the lower of the ask and its average of the last seconds, the way the live order does (restprice.py)
                price = restprice.rest_price(spec, r["side"], float(r["ask"]), [(q[0], q[1], q[2]) for q in quotes if q[0] <= r["ts"]], float(r["ts"]))

            def side_ask(q: Any, side: str = r["side"]) -> float:
                return float(q[2]) if side == "yes" else 1.0 - float(q[1])

            def side_bid_q(q: Any, side: str = r["side"]) -> float | None:
                return restprice.side_bid(float(q[1]), float(q[2]), side)

            at_order = [q for q in quotes if q[0] <= r["ts"]]
            if spec.buy_rest_at_bid and at_order:  # the order sits on the side's bid at the decision (restprice.py); the tape judges it filled when the ask comes down to it
                bid_at = restprice.side_bid(float(at_order[-1][1]), float(at_order[-1][2]), r["side"])
                if bid_at is not None:
                    price = restprice.rest_price(spec, r["side"], float(r["ask"]), [], float(r["ts"]), bid=bid_at)
            crossed = bool(at_order) and side_ask(at_order[-1]) <= price + 1e-9
            later = [q for q in quotes if r["ts"] < q[0] <= r["ts"] + window]
            flows = tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts > ? AND ts <= ?", (r["ticker"], int(r["ts"]), int(r["ts"] + window))).fetchall()
            start = price
            moves = [(float(r["ts"]), price)]  # (when, where the order sat from then): one entry unless the lane follows the ask down
            if spec.buy_rest_follow:
                price, moves, withdrawn = _follow_down(spec, later, side_ask, price, float(r["ts"]), side_bid_q)
                if withdrawn:
                    later = []  # the live lane cancels an order whose ask fell under the lane's floor and does not place it again
                    flows = []
            fill = next((_sat(moves, q[0]) for q in later if side_ask(q) <= _sat(moves, q[0]) + 1e-9), None)
            if fill is None:  # a seller can hit our bid while the ask stays above it: a trade by a taker against our side at our price or better
                for fl in flows:
                    if fl[3] is None:
                        continue
                    at = _sat(moves, fl[0])
                    if (r["side"] == "yes" and fl[2] > 0 and fl[3] <= at + 1e-9) or (r["side"] == "no" and fl[1] > 0 and fl[3] >= 1.0 - at - 1e-9):  # NO takers sell YES into our bid; YES takers lift the YES ask we posted
                        fill = at
                        break
            came = fill is not None
            if came:
                price = fill  # type: ignore[assignment]
            reason = (r["reason"] or "").split(" " + OLD_MARK)[0].split(" " + MARK)[0]
            if not crossed and came:
                stake = spec.fixed_stake or float(r["stake_usd"] or 0.0)
                if r["action"] == "pass" and stake <= 0:
                    continue  # cannot restore the size of a bet the first version threw out
                n = round(stake / (price + fee_per_contract(price)), 2) if r["action"] == "pass" else None
                store.db.execute(
                    "UPDATE decisions SET action='bet', ask=?, contracts=COALESCE(?, contracts), stake_usd=COALESCE(?, stake_usd), reason=? WHERE id=?",
                    (price, n, round(stake, 4) if n is not None else None, f"{reason} {MARK}: a resting bid at {price:.2f} would have filled{f' (it followed the ask down from {start:.2f})' if abs(price - start) > 1e-9 and spec.buy_rest_follow else ''}]", r["id"]),
                )
                out["filled"] += 1
            elif spec.buy_rest_then_take and not crossed and (follow := _take_after_rest(spec, r, quotes, window)) is not None:
                stake = spec.fixed_stake or float(r["stake_usd"] or 0.0)
                n = round(stake / (follow + fee_per_contract(follow)), 2) if r["action"] == "pass" and stake > 0 else None
                store.db.execute(
                    "UPDATE decisions SET action='bet', ask=?, contracts=COALESCE(?, contracts), stake_usd=COALESCE(?, stake_usd), reason=? WHERE id=?",
                    (follow, n, round(stake, 4) if n is not None else None, f"{reason} {MARK}: the resting bid at {price:.2f} did not fill, the live lane then takes the ask: bought at {follow:.3f}]", r["id"]),
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
        store.touch("bets")
    return out


def _sat(moves: list[tuple[float, float]], when: float) -> float:
    """Where the resting order sat at `when`: the price of the last move at or before it."""
    out = moves[0][1]
    for at, p in moves:
        if at <= when:
            out = p
    return out


def _follow_down(spec: Any, later: list[Any], side_ask: Any, price: float, started: float, side_bid: Any = None) -> tuple[float, list[tuple[float, float]], bool]:
    """The live follow rule (runner._rest_follow, lane field buy_rest_follow) on the tape's quotes: each quote asks where the order should sit, `buy_rest_below` under the ask; when that is lower than the order it moves there (at most REST_FOLLOW_MAX times), a risen ask never raises it, and an ask under the lane's price floor withdraws it. An order that the ask reached before the quote that would have moved it stays where it was. Returns (the order's last price, its moves as (when, price), withdrawn)."""
    from .runner import REST_FOLLOW_MAX

    moves = [(started, price)]
    follows = 0
    for q in later:
        a = side_ask(q)
        if a <= price + 1e-9:
            break  # filled here at the order's price
        if a < spec.price[0] - 1e-9:
            return price, moves, True
        want = round(max(0.01, a - spec.buy_rest_below), 4)
        if spec.buy_rest_at_bid and side_bid is not None:  # the order follows the side's bid down
            b = side_bid(q)
            if b is None:
                continue
            want = round(max(0.01, b), 4)
        if follows < REST_FOLLOW_MAX and want < price - 1e-9:
            price = want
            follows += 1
            moves.append((float(q[0]), price))
    return price, moves, False


def _take_after_rest(spec: Any, r: Any, quotes: Any, window: float) -> float | None:
    """The price the live lane's fallback buy would have got (buy_rest_then_take): after the rest window the ask is read again and bought if it is inside the lane's price band and not more than buy_retry_chase over the ask
    the bet was decided at. None when it would not have been (the market ran away, or no quote came)."""
    after = [q for q in quotes if q[0] >= r["ts"] + window]
    if not after:
        return None
    q = after[0]
    a = float(q[2]) if r["side"] == "yes" else 1.0 - float(q[1])
    if getattr(spec, "price_skip", (0.0, 0.0))[1] > 0 and spec.price_skip[0] - 1e-9 <= a <= spec.price_skip[1] + 1e-9:
        return None  # the lane's no-buy range, as the live fallback
    if a < spec.price[0] - 1e-9 or a > spec.price[1] + 1e-9 or a > float(r["ask"]) + max(0.0, spec.buy_retry_chase) + 1e-9:
        return None
    return round(a, 4)


def settle_taker(s: Settings, store: Store, tape: Any, now: float | None = None) -> dict[str, int]:
    """Judge the paper bets of a taker lane that sets paper_fill_model, once their quotes are on the tape: a bet whose side's ask, at the decision, was within the lane's limit (the decision price plus buy_slip) is refilled at that ask;
    one whose ask was over the limit becomes a pass. Live lanes are never touched. Returns {"filled": n, "missed": n}."""
    now = time.time() if now is None else now
    out = {"filled": 0, "missed": 0}
    specs = {name: sp for name, sp in lane_specs(s).items() if sp.paper_fill_model and sp.buy_rest_seconds <= 0}
    if not specs or tape is None:
        return out
    for lane, spec in specs.items():
        rows = store.db.execute(
            f"""SELECT id, ts, ticker, side, ask, reason, stake_usd FROM decisions
                WHERE lane=? AND mode='observe' AND action='bet' AND exit_ts IS NULL AND ts < ? AND ts > ?
                  AND COALESCE(reason,'') NOT LIKE '%{MARK_FM}%' AND COALESCE(reason,'') NOT LIKE '%live order not filled%'""",
            (lane, now - GRACE, now - 24 * 3600),
        ).fetchall()
        for r in rows:
            if s.is_live(lane, r["ticker"].split("-")[0]):
                continue  # a live lane's bets were real orders
            quotes = tape.execute(
                "SELECT ts, bid, ask, bid_size, ask_size FROM quotes WHERE ticker=? AND ts >= ? AND ts <= ? AND bid > 0 AND ask > 0 ORDER BY ts",
                (r["ticker"], r["ts"] - 1.0, r["ts"] + 5.0),
            ).fetchall()
            if not quotes:
                continue
            fill = simulate_taker_buy([tuple(float(x or 0.0) for x in q) for q in quotes], r["side"], float(r["ts"]), float(r["ask"]), spec.buy_slip)
            reason = r["reason"] or ""  # the whole note (2026-11-19: it was cut to 230 characters, which dropped the chain note and every shadow verdict at its end)
            if fill.filled and fill.price is not None:
                stake = spec.fixed_stake or float(r["stake_usd"] or 0.0)
                n = round(stake / (fill.price + fee_per_contract(fill.price)), 2) if stake > 0 else None
                store.db.execute(
                    "UPDATE decisions SET ask=?, contracts=COALESCE(?, contracts), reason=? WHERE id=?",
                    (fill.price, n, f"{reason} {MARK_FM}: filled at {fill.price:.3f} (logged {float(r['ask']):.3f})]", r["id"]),
                )
                out["filled"] += 1
            else:
                store.db.execute(
                    "UPDATE decisions SET action='pass', contracts=NULL, stake_usd=NULL, outcome=NULL, reason=? WHERE id=?",
                    (f"{reason} {MARK_FM}: not filled, the ask was over the limit {float(r['ask']) + spec.buy_slip:.3f}]", r["id"]),
                )
                out["missed"] += 1
        store.db.commit()
        store.touch("bets")
    return out
