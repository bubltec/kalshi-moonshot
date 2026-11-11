"""Scorecard for the Kalshi-spot curve logic (2026-11-11, user: "lets keep an eye on the kalshi curve logic and apply it as soon as we have positive results").
The Kalshi-first reading (lane field spot_source = "kalshi": Kalshi's perp reference price as the spot for the curve) runs on two paper shadows. This prints where each stands against a bar fixed BEFORE results (CLAUDE.md rules 4 to 6):
  bar to apply it to a live lane, all four needed, fees included, on the shadow's own settled bets from the date shown:
    1. at least 30 settled bets;
    2. positive net dollars as it happened (a resting-ask fill at its price with no fee; any other sale at the bid less the 0.02 slip and the taker fee; unsold bets at settlement);
    3. positive in BOTH halves of its bets (by time);
    4. where a live twin exists, no worse than the twin on the markets both bought (at least 10 matched bets), per dollar staked.
  A shadow that turns negative over its last 20 bets is flagged RETIRE-CHECK (rule 6).
Attribution is a separate warning: a shadow that also sells differently from its twin cannot show the spot is what made the difference. The 'attribution' line says what differs.
Starts: commodity_multiscale_rest 2026-11-11 16:45 ET (new band, stake and hot gate matched to the live lane); index_wave 11:23 (its first bet). A clean twin of the live lane that changes only the spot would carry the attribution; the script lists any lane named *_kalshi.
    scripts/cloud.sh run scripts/kalshi_curve_scorecard.py        (or rpy.sh)
"""

import datetime as dt
import time

from kalshi_moonshot import config
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
ET = dt.timezone(dt.timedelta(hours=-5))
day = dt.datetime(2026, 11, 11, tzinfo=ET)
at = lambda h, m, sec=0: day.replace(hour=h, minute=m, second=sec).timestamp()  # noqa: E731
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
CARDS = [("commodity_multiscale_rest", "commodity_multiscale", at(16, 45)), ("index_wave", None, at(11, 23))]
NEED_N, NEED_MATCHED = 30, 10


def rows(lane, since):
    return st.db.execute("SELECT ts, ticker, side, ask, stake_usd, outcome, exit_price, exit_reason FROM decisions WHERE lane=? AND action='bet' AND ts>=? AND stake_usd>0 AND ask>0 ORDER BY ts", (lane, since)).fetchall()


def dollars(r):
    """Dollars as it happened, or None while the bet is open."""
    ask, stake = float(r["ask"]), float(r["stake_usd"])
    cost = ask + fee(ask)
    if r["exit_price"] is not None:
        px = float(r["exit_price"])
        back = px if str(r["exit_reason"] or "").startswith("curve exit (resting ask)") else px - EXIT_SLIP - fee(px)
    elif r["outcome"] in ("yes", "no"):
        back = 1.0 if r["outcome"] == r["side"] else 0.0
    else:
        return None
    return stake * (back / cost - 1.0)


def summary(rs):
    done = [(r, dollars(r)) for r in rs if dollars(r) is not None]
    stake = sum(float(r["stake_usd"]) for r, _ in done)
    tot = sum(d for _, d in done)
    half = len(done) // 2
    a, b = sum(d for _, d in done[:half]), sum(d for _, d in done[half:])
    return dict(n=len(done), open=len(rs) - len(done), tot=tot, stake=stake, per=(tot / stake if stake else 0.0), h1=a, h2=b, last20=sum(d for _, d in done[-20:]), n20=min(20, len(done)))


def show(label, m):
    if not m["n"]:
        return f"  {label:12s} n=0 (open {m['open']})"
    return f"  {label:12s} n={m['n']:3d} (open {m['open']}) | {m['tot']:+7.2f} on ${m['stake']:6.2f} ({m['per']:+.1%}) | halves {m['h1']:+.2f} / {m['h2']:+.2f}"


print(f"run {hm(time.time())} ET")
for lane, twin, since in CARDS:
    rs = rows(lane, since)
    m = summary(rs)
    print(f"\n{lane}  (bets from {hm(since)} ET)")
    print(show("shadow", m))
    checks = [(f"1. at least {NEED_N} settled bets", m["n"] >= NEED_N, f"has {m['n']}"), ("2. net positive", m["tot"] > 0, f"{m['tot']:+.2f}"), ("3. both halves positive", m["h1"] > 0 and m["h2"] > 0, f"{m['h1']:+.2f} / {m['h2']:+.2f}")]
    if twin:
        tw = rows(twin, since)
        keys = {(r["ticker"], r["side"]) for r in tw}
        mm = summary([r for r in rs if (r["ticker"], r["side"]) in keys])
        tm = summary([r for r in tw if (r["ticker"], r["side"]) in {(x["ticker"], x["side"]) for x in rs}])
        print(show("matched", mm) + "   <- shadow on markets the twin also bought")
        print(show("twin there", tm) + f"   <- {twin} on those same markets")
        ok = mm["n"] >= NEED_MATCHED and mm["per"] >= tm["per"]
        checks.append((f"4. no worse than {twin} on matched markets (n >= {NEED_MATCHED})", ok, f"matched n={mm['n']}, {mm['per']:+.1%} vs {tm['per']:+.1%}"))
    for label, ok, detail in checks:
        print(f"   [{'x' if ok else ' '}] {label}: {detail}")
    verdict = "READY to apply (the live switch stays yours)" if all(c[1] for c in checks) else "NOT YET"
    if m["n"] >= 20 and m["last20"] < 0:
        verdict += "; RETIRE-CHECK: negative over its last 20 bets"
    print(f"   verdict: {verdict}")
# attribution: what differs from the twin besides the spot
import dataclasses

from kalshi_moonshot.lanes import lane_specs

L = lane_specs(s)
print("\nattribution (what differs from the live twin, besides the spot)")
skip = {"name", "label", "priority", "enabled"}
for lane, twin in (("commodity_multiscale_rest", "commodity_multiscale"),):
    a, b = L[twin], L[lane]
    d = [f.name for f in dataclasses.fields(a) if f.name not in skip and getattr(a, f.name) != getattr(b, f.name)]
    print(f"  {lane}: differs in {', '.join(d)}")
clean = [n for n in L if n.endswith("_kalshi") and L[n].enabled]
print("  clean one-change twins (named *_kalshi):", ", ".join(clean) if clean else "none yet")
print("DONE")
