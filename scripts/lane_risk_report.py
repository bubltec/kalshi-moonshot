"""WHICH LANES HAVE AN EDGE WORTH SIZING: a risk-adjusted scorecard per lane from the settled bets, in the language of the standard trading metrics.

For every bet that has finished (sold, or the window settled), the return per dollar staked: (what it paid back after the sale fee, or 1 / 0 at settlement) minus the price and the entry fee, over the price and the entry fee. Per lane, the last DAYS days:
  mean return        the average return per dollar staked, with its standard error (is it distinguishable from zero?)
  Sharpe-like        mean / standard deviation of the per-bet returns (not annualised: a comparison between lanes, not a headline)
  Sortino-like       mean / downside deviation, which penalises only the losing bets (the right yardstick for a lane that wins rarely and big)
  max drawdown       the deepest fall in the running total of per-bet returns, in dollars staked
  full Kelly         the fraction of a bankroll that maximises the average log growth on THESE returns (the empirical Kelly fraction, found numerically). 0 means no
                     measured edge to size; the bot stakes KELLY_FRACTION (a quarter) of it, and a lane's fixed_stake ignores it.
A lane with a mean inside its error bars has no measured edge, and Kelly will say so by returning 0. Live (mode=trade) bets are listed apart from paper.
Read-only. Places nothing.
    docker compose exec -T bot python - [days] < scripts/lane_risk_report.py
"""
import math
import statistics
import sys
import time

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

DAYS = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0
MIN_BETS = 20
s = config.reload()
main = Store(s.db_path, s.exclude_versions)
since = time.time() - DAYS * 86400
rows = main.db.execute(
    """SELECT d.lane, d.mode, d.ts, d.ask, d.side, d.exit_price, d.contracts, d.stake_usd, w.result FROM decisions d LEFT JOIN windows w ON w.ticker = d.ticker
       WHERE d.action = 'bet' AND d.ask > 0 AND d.ts > ? AND (d.exit_price IS NOT NULL OR w.result IN ('yes','no')) ORDER BY d.ts""", (since,)).fetchall()


def dollars(r):
    """The bet's profit or loss in dollars: its return per dollar times the dollars it staked (stake_usd, which matches the order records; the decision's contract count does not always)."""
    return per_dollar(r) * float(r["stake_usd"] or 0.0)


def per_dollar(r):
    ask = float(r["ask"])
    cost = ask + fee(ask)  # the quote plus the taker fee, for paper and live alike. Live fills in the order records cost about the quote or a little under (0.79 against 0.80 on crypto_favorite), so this is slightly hard on a live lane
    if r["exit_price"] is not None:
        back = max(float(r["exit_price"]), 0.0) - (fee(float(r["exit_price"])) if float(r["exit_price"]) > 0 else 0.0)
    else:
        back = 1.0 if r["result"] == r["side"] else 0.0
    return (back - cost) / cost


def kelly(returns):
    """The fraction f in [0, 1) maximising the mean of log(1 + f r), by a grid search: the empirical Kelly fraction."""
    best_f, best = 0.0, 0.0
    for k in range(1, 400):
        f = k / 400.0
        if any(1.0 + f * r <= 0 for r in returns):
            break
        g = statistics.mean(math.log(1.0 + f * r) for r in returns)
        if g > best:
            best_f, best = f, g
    return best_f


def drawdown(returns):
    peak = total = worst = 0.0
    for r in returns:
        total += r
        peak = max(peak, total)
        worst = min(worst, total - peak)
    return worst


def summarize(label, rs, net):
    n = len(rs)
    if n < MIN_BETS:
        return None
    m = statistics.mean(rs)
    sd = statistics.pstdev(rs)
    down = math.sqrt(statistics.mean(min(r, 0.0) ** 2 for r in rs))
    se = sd / math.sqrt(n)
    return (label, n, m, se, m / sd if sd else float("nan"), m / down if down else float("nan"), drawdown(rs), kelly(rs), net)


print(f"Settled bets in the last {DAYS:g} days (sold or settled), per dollar staked, fees paid. Lanes under {MIN_BETS} bets are left out.\n")
print(f"{'lane':24} {'bets':>5} {'mean':>8} {'+-':>6} {'verdict':>10} {'Sharpe':>7} {'Sortino':>8} {'max dd':>8} {'full Kelly':>11} {'stake at 1/4':>13} {'net $':>9}")
out = []
for lane in sorted({r["lane"] or "moonshot" for r in rows}):
    for mode_label, pick in (("live", lambda r: r["mode"] == "trade"), ("paper", lambda r: r["mode"] != "trade")):
        chosen = [r for r in rows if (r["lane"] or "moonshot") == lane and pick(r)]
        res = summarize(f"{lane} ({mode_label})", [per_dollar(r) for r in chosen], sum(dollars(r) for r in chosen))
        if res:
            out.append(res)
for label, n, m, se, sh, so, dd, k, net in sorted(out, key=lambda x: -x[2]):
    verdict = "edge" if m - 2 * se > 0 else ("LOSING" if m + 2 * se < 0 else "no edge")
    print(f"{label:24} {n:>5} {m:>+8.1%} {100 * se:>5.1f}% {verdict:>10} {sh:>7.2f} {so:>8.2f} {dd:>+8.2f} {k:>10.0%} {k / 4:>12.1%} {net:>+9.2f}")
print("\nverdict: 'edge' when the mean is more than 2 standard errors above zero, 'LOSING' when more than 2 below, otherwise no measured edge (the error bars include zero).")
