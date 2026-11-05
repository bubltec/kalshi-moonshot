"""Does each lane and each rule keep the promise that got it switched on?

Every edge in this project began as a backtest on a few days of tape, and several have faded when the sample grew (the 93% win rate of calm Treasury favorites was 86% on 300 favorites, and 79% in the second half). This keeps the
claim next to the live record: for each lane or rule, the date it went live, the figure that justified it, and what the live bets have done since. A rule that refuses bets is also scored on what the refused ones would have
done at their logged price, which is the only way to tell whether it is saving money or only sitting out. Read-only: python -m kalshi_moonshot scorecard [--days N].
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .store import Store
from .strategy import fee_per_contract


def utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> float:
    return float(calendar.timegm((y, mo, d, h, mi, 0)))


@dataclass(frozen=True)
class Claim:
    id: str
    kind: str  # "lane_roi": live bets of the lanes, net / staked. "refused_ev10": what refused candidates would have made on a $10 ticket. "exit_vs_hold": sold minus hold, per sale
    lanes: tuple[str, ...]
    adopted: float  # epoch seconds (UTC): only decisions after this count
    claim: float  # the backtest figure; its sign is the promise (positive = it earns, negative = what it refuses loses)
    text: str  # what was claimed, in words
    source: str  # where the backtest is written down
    min_n: int = 30
    reason_key: str = ""  # a refusal's reason contains this ("refused_ev10") or a bet's note does ("lane_roi" with a key)
    reason_exclude: str = ""
    exit_key: str = ""  # exit_reason starts with this ("exit_vs_hold")


# The registry. Add a line when a rule or lane goes live; the figure is what justified it, not what it has done since.
CLAIMS: tuple[Claim, ...] = (
    Claim("wild-fade", "lane_roi", ("crypto_wild_fade",), utc(2026, 11, 4, 18, 30), 0.296, "underdog of a 0.82+ favorite in a wild regime (z>=1, not far-and-flat): +$2.96 a $10 bet, wins 15.6% at ~11c",
          "studies/results/2026-11-04-crypto-vol-regime.txt", 30),
    Claim("treasury-calm", "lane_roi", ("treasury_favorite",), utc(2026, 11, 4, 14, 0), 0.07, "calm (no 50c crossing) treasury favorites: 93% won on 88, +$0.64 a $10 bet (on 300 favorites: 86%, -$0.10)",
          "studies/results/2026-11-04-treasury-favorites.txt", 30),
    Claim("commodity-vol-gate", "refused_ev10", ("commodity_energy", "commodity_precious", "commodity_industrial"), utc(2026, 11, 4, 20, 45), -0.37,
          "commodity favorites refused at 1.3x normal volatility or more lose $0.37 a $10 bet", "studies/results/2026-11-04-gradual-erosion.txt", 30, "outside the lane", "choppy"),
    Claim("crypto-vol-gate", "refused_ev10", ("crypto_favorite", "crypto_btc", "crypto_sol"), utc(2026, 11, 4, 23, 30), -0.25,
          "crypto favorites refused at 1.3x normal volatility or more lose $0.25 a $10 bet", "studies/results/2026-11-04-crypto-vol-regime.txt", 30, "outside the lane", ""),
    Claim("market-gate", "refused_ev10", ("commodity_energy", "commodity_precious", "commodity_industrial"), utc(2026, 11, 4, 14, 45), -0.58,
          "markets the 48 h ledger shows losing (natural gas, palladium, platinum, gold) lose $0.58 a $10 bet when bought anyway", "walk-forward on the 2.3 day ledger, 2026-11-04", 30, "market skipped", ""),
    Claim("variety-skip-crypto", "refused_ev10", ("crypto_favorite", "crypto_btc", "crypto_sol"), utc(2026, 11, 4, 18, 30), -0.9,
          "crypto varieties losing in both halves (F-yes: -$54 and -$64 on 100) lose when bought", "studies/results/2026-11-04-crypto-vol-regime.txt", 30, "does not buy it", "against"),
    Claim("spot-center-quarter", "lane_roi", ("commodity_precious", "commodity_industrial"), utc(2026, 11, 5, 0, 15), -0.10,
          "bets where the spot and the corrected center disagree about the strike lose about 10% of their stake at full size (94 bets, -$54); played at a quarter", "2026-11-04 audit of 672 live commodity bets", 20, "[stake x0.25"),
    Claim("shock-exit", "exit_vs_hold", ("commodity_energy", "commodity_precious", "commodity_industrial"), utc(2026, 11, 3, 23, 0), 0.79,
          "the shock exit earns $0.79 a sale against holding (58 sales, +$45.8 on 297 metal bets)", "studies/results/2026-11-04-shock-bid-metals.txt", 15, exit_key="shock exit"),
    Claim("strike-through-eth-doge", "exit_vs_hold", ("crypto_favorite", "crypto_sol"), utc(2026, 11, 3, 23, 0), 0.12,
          "the strike-through sale earns $0.12 a sale against holding (66 sales, +$8.1 on 332 ETH/DOGE bets)", "studies/results/2026-11-04-strike-through-eth-doge.txt", 15, exit_key="strike-through"),
)


def ticket_pnl(ask: float, won: bool, stake: float = 10.0) -> float:
    """What a bought-at-the-ask ticket makes at its result, with the taker fee."""
    c = stake / ask
    return (c * (1.0 - ask) if won else -stake) - c * fee_per_contract(ask)


def bet_pnl(ask: float, stake: float, won: bool, exit_price: float | None) -> float:
    c = stake / ask
    if exit_price is not None:
        return c * (exit_price - ask) - c * fee_per_contract(max(exit_price, 0.01)) - c * fee_per_contract(ask)
    return c * (1.0 - ask) - c * fee_per_contract(ask) if won else -stake


def evaluate(claim: Claim, rows: list[dict]) -> dict:
    """The live figure for one claim from decision rows (lane, ts, action, mode, side, ask, stake_usd, exit_price, exit_reason, reason, ticker, result)."""
    mine = [r for r in rows if r["lane"] in claim.lanes and r["ts"] > claim.adopted and r["result"] in ("yes", "no") and r["ask"]]
    out: dict[str, Any] = {"n": 0, "live": None, "wins": 0}
    if claim.kind == "lane_roi":
        sel = [r for r in mine if r["action"] == "bet" and r["mode"] == "trade" and r["stake_usd"] and (not claim.reason_key or claim.reason_key in (r["reason"] or ""))]
        staked = sum(r["stake_usd"] for r in sel)
        net = sum(bet_pnl(r["ask"], r["stake_usd"], r["result"] == r["side"], r["exit_price"]) for r in sel)
        out.update(n=len(sel), wins=sum(1 for r in sel if r["result"] == r["side"]), live=(net / staked if staked else None), net=net, staked=staked)
    elif claim.kind == "refused_ev10":
        seen: set[tuple] = set()
        sel = []
        for r in sorted(mine, key=lambda r: r["ts"]):
            if r["action"] != "pass" or claim.reason_key not in (r["reason"] or "") or (claim.reason_exclude and claim.reason_exclude in (r["reason"] or "")):
                continue
            key = (r["lane"], r["ticker"], r["side"])
            if key in seen:
                continue
            seen.add(key)
            sel.append(r)
        pnls = [ticket_pnl(r["ask"], r["result"] == r["side"]) for r in sel]
        out.update(n=len(sel), wins=sum(1 for r in sel if r["result"] == r["side"]), live=(sum(pnls) / len(pnls) if pnls else None), net=sum(pnls))
    elif claim.kind == "exit_vs_hold":
        sel = [r for r in mine if r["action"] == "bet" and r["mode"] == "trade" and r["exit_price"] is not None and (r["exit_reason"] or "").startswith(claim.exit_key) and r["stake_usd"]]
        diffs = [bet_pnl(r["ask"], r["stake_usd"], r["result"] == r["side"], r["exit_price"]) - bet_pnl(r["ask"], r["stake_usd"], r["result"] == r["side"], None) for r in sel]
        out.update(n=len(sel), wins=sum(1 for d in diffs if d > 0), live=(sum(diffs) / len(diffs) if diffs else None), net=sum(diffs))
    return out


def verdict(claim: Claim, got: dict) -> str:
    """'too early' under min_n; 'holding' when the live figure is on the claim's side and at least half of it; 'not holding' when it is on the other side; else 'weaker'."""
    if got["n"] < claim.min_n or got["live"] is None:
        return "too early"
    live, c = got["live"], claim.claim
    if live * c <= 0:
        return "not holding"
    return "holding" if abs(live) >= abs(c) * 0.5 else "weaker"


def report(s: Settings, days: float = 14.0, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    store = Store(s.db_path, s.exclude_versions)
    first = min(c.adopted for c in CLAIMS)
    cutoff = max(first, now - days * 86400.0)
    rows = [dict(r) for r in store.db.execute(
        "SELECT d.lane, d.ts, d.action, d.mode, d.side, d.ask, d.stake_usd, d.exit_price, d.exit_reason, d.reason, d.ticker, w.result "
        "FROM decisions d LEFT JOIN windows w ON w.ticker = d.ticker WHERE d.ts > ?", (cutoff,)).fetchall()]
    out = ["Does each lane and rule keep the promise that got it switched on? (live decisions since the date it went live; refused candidates are valued as if bought at their logged price on a $10 ticket)",
           f"{'rule':24} {'went live (UTC)':16} {'n':>4} {'live':>9} {'claimed':>9}  verdict",
           ]
    for c in CLAIMS:
        got = evaluate(c, rows)
        unit = "ROI" if c.kind == "lane_roi" else "$/ticket" if c.kind == "refused_ev10" else "$/sale"
        live = "-" if got["live"] is None else (f"{got['live'] * 100:+.1f}%" if c.kind == "lane_roi" else f"{got['live']:+.2f}")
        claimed = f"{c.claim * 100:+.1f}%" if c.kind == "lane_roi" else f"{c.claim:+.2f}"
        out.append(f"{c.id:24} {time.strftime('%m-%d %H:%M', time.gmtime(c.adopted)):16} {got['n']:4d} {live:>9} {claimed:>9}  {verdict(c, got)} ({unit}, {got['wins']} wins)")
    out.append("")
    for c in CLAIMS:
        out.append(f"  {c.id}: {c.text} [{c.source}]")
    return out
