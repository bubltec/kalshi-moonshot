"""Shadow lanes against their live twins, on bets a real order could have made (2026-11-11, user: "Yes, build it").

Question: the paper shadows score paper bets, and a paper bet can be one a live order would never have filled (a resting buy that did not fill, an ask that left the band, no offers). Over the shadow lanes' own bets
since their sale logic ran, what do they make, how many of those bets are ones the tape's fill model says would have filled, and on the markets where the live twin also bought, who made more?

Lanes and where each one's clean data starts (fixed before results; 2026-11-11 ET, EST = UTC-5):
  crypto_multiscale_rest, commodity_multiscale_rest, crypto_multiscale_underdog, index_wave: from 11:00 (the takes_profit fix, deployed 10:59:40, after which their resting sale and guard ran)
  crypto_multiscale_comeback: from 12:44:40 (its hold with an end, deployed at that mark)
  twins: crypto_multiscale for the crypto shadows, commodity_multiscale for the commodity one; underdog and index_wave have none.

Three views of each shadow lane, all scored as the lane's own sale made them (a resting-ask fill at its price with no fee; any other sale at the bid less the slip and the taker fee; unsold bets at settlement), against holding the same bets:
  all      every bet the lane logged;
  judged   bets whose reason carries the tape fill judgment (paperfill.py: "would have filled" or the taker follow-up); a bet judged as not filling is already a pass;
  matched  bets on a market and side the live twin also bought (the paired view: the shadow's result and the twin's on the same markets).
Reported per view: n, bets still open (not counted), dollars, dollars per $ staked, wins, dollars if held, and the same in two halves of the lane's own period. Dollars use each bet's stake_usd.
The curve-shape split the study rules ask for is not done: a few dozen bets cannot carry it.

    scripts/cloud.sh run scripts/shadow_compare.py
"""

import datetime as dt
import time

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
ET = dt.timezone(dt.timedelta(hours=-5))
day = dt.datetime(2026, 11, 11, tzinfo=ET)
at = lambda h, m, sec=0: day.replace(hour=h, minute=m, second=sec).timestamp()  # noqa: E731
LANES = [
    ("crypto_multiscale_rest", "crypto_multiscale", at(11, 0)),
    ("commodity_multiscale_rest", "commodity_multiscale", at(11, 0)),
    ("crypto_multiscale_underdog", None, at(11, 0)),
    ("index_wave", None, at(11, 0)),
    ("crypto_multiscale_comeback", "crypto_multiscale", at(12, 44, 40)),
]
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%H:%M")  # noqa: E731
JUDGED = ("would have filled]", "then takes the ask")


def bets(lane, since):
    return st.db.execute(
        "SELECT id, ts, ticker, side, ask, stake_usd, outcome, exit_price, exit_reason, reason FROM decisions WHERE lane=? AND action='bet' AND ts>=? ORDER BY ts", (lane, since)
    ).fetchall()


def score(r):
    """(as traded, held) in dollars, or (None, None) while the bet is open."""
    ask, stake = float(r["ask"] or 0.0), float(r["stake_usd"] or 0.0)
    if ask <= 0 or stake <= 0:
        return None, None
    cost = ask + fee(ask)
    won = None if r["outcome"] not in ("yes", "no") else (1.0 if r["outcome"] == (r["side"] or "yes") else 0.0)
    if r["exit_price"] is not None:
        px = float(r["exit_price"])
        back = px if str(r["exit_reason"] or "").startswith("curve exit (resting ask)") else px - EXIT_SLIP - fee(px)
    elif won is not None:
        back = won
    else:
        return None, None
    held = None if won is None else stake * (won / cost - 1.0)
    return stake * (back / cost - 1.0), held


def line(label, rows):
    got = [(r, *score(r)) for r in rows]
    done = [g for g in got if g[1] is not None]
    openn = len(got) - len(done)
    if not done:
        return f"  {label:8s} n=0 (open {openn})"
    tot = sum(g[1] for g in done)
    stake = sum(float(g[0]["stake_usd"]) for g in done)
    held_rows = [g for g in done if g[2] is not None]
    held = sum(g[2] for g in held_rows)
    wins = sum(1 for g in done if g[1] > 0)
    half = len(done) // 2
    a, b = done[:half], done[half:]
    halves = f"halves {sum(g[1] for g in a):+.2f} / {sum(g[1] for g in b):+.2f}" if half else "halves n/a"
    return f"  {label:8s} n={len(done):3d} (open {openn}) | {tot:+7.2f} on ${stake:6.2f} staked ({tot / stake:+.1%}) | {wins}/{len(done)} ahead | held {held:+7.2f} on {len(held_rows)} settled | {halves}"


print(f"run {hm(time.time())} ET")
for lane, twin, since in LANES:
    rows = bets(lane, since)
    print(f"\n{lane}  (from {hm(since)} ET; {len(rows)} bets)")
    print(line("all", rows))
    print(line("judged", [r for r in rows if any(m in (r["reason"] or "") for m in JUDGED)]))
    if twin:
        tw = bets(twin, since)
        keys = {(r["ticker"], r["side"]) for r in tw}
        matched = [r for r in rows if (r["ticker"], r["side"]) in keys]
        print(line("matched", matched))
        mk = {(r["ticker"], r["side"]) for r in matched}
        print(line("twin", [r for r in tw if (r["ticker"], r["side"]) in mk]) + f"   <- {twin} on those same markets")
        print(line("twin all", tw) + f"   <- every {twin} bet in the period")
    # how the lane's bets ended
    ends = {}
    for r in rows:
        k = "open" if score(r)[0] is None else (str(r["exit_reason"] or "settled")[:34] if r["exit_price"] is not None else "held to settlement")
        ends[k] = ends.get(k, 0) + 1
    print("  ended:", "; ".join(f"{v} {k}" for k, v in sorted(ends.items(), key=lambda x: -x[1])))
print("\nDONE")
