"""The order of the buy's pieces, replayed on the archived tape (2026-11-17, user: "ladder should not be the entry point"; "the ladder is something that should come LAST in the chain. all it does is evaluate the worth of the bet, not what side to pick or whether it makes any sense"; "the risk score makes way more sense to go FIRST"; "i want to run a simulation on this"; "just move the pieces we have around").

One question on the same moments: if the risk score picks the side and the ladder only checks the worth at the end, what would the live crypto lane have bought, against what it buys when the ladder picks the side (as now)?

Same moments: data/tape_archive/replay_readings.csv (scripts/risk_attribute_replay.py build): every 30 s mark of every 15-minute market since Mon 2026-11-09 with Kalshi's quote and the risk score's readings, here the lane's own series only. A bet buys a side at its ask and holds it to settlement, fee in; one bet per market (the first mark that qualifies); results per dollar staked and on $5 stakes.
Every order applies the lane's own price range and no-buy range to the side it buys (crypto_multiscale in data/strategies.toml). No other gate of the lane is replayed (the trend check, the flip rule, the same-side cap, the swing range, the curve's chance cap), and no exit: held to settlement.

The pieces, as they exist:
  ladder     the worth of a side: the normal chance from the cushion (the index against the target in its typical moves for the time left: the replay's `cushion` reading). STAND-IN: the live ladder reads the Kalshi perp, the last hour's rate and an hourly correction; this is the same form from the settlement index and the hour before the open. It is not the live number.
             The lane's edge rule on it: lanes.wants_bet, the edge at least min_edge_room of the room to a sure win.
  risk score riskscore.py's chance for the side from the seven readings it adds (riskscore.USED), refitted WALK-FORWARD here (on every earlier day, used on the next), since the live weights were fitted on these same days. Held: riskscore.low_risk at this mark, 30 s ago and 60 s ago.

The orders:
  now        ladder first: the side whose worth clears the edge rule (at most one side can).
  reordered  risk score first: the side whose risk is at or under a level and has held. Two levels, both existing: under even (0.5: the side the score favours) and the paper lane crypto_risk_score's 0.25.
             then the ladder LAST, three ways: no worth check; worth at least the cost (edge over 0); the lane's own edge bar.
Settings: none chosen after seeing the output. By curve shape: the cushion zone the score itself uses (on the line / fair / wide).
Run on the Mac: .venv/bin/python scripts/entry_order_sim.py > studies/results/<date>-entry-order-sim.txt"""
import csv
import datetime
import importlib.util
import math
import os
from collections import defaultdict
from zoneinfo import ZoneInfo

import numpy as np

os.environ.setdefault("DATA_DIR", "data")
from kalshi_moonshot import config, lanes, riskscore  # noqa: E402
from kalshi_moonshot.strategy import fee_per_contract  # noqa: E402

_spec = importlib.util.spec_from_file_location("replay", os.path.join(os.path.dirname(__file__), "risk_attribute_replay.py"))
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)

ET = ZoneInfo("America/New_York")
LANE = "crypto_multiscale"
s = config.load()
spec = lanes.spec_for(s, LANE)
series = set(spec.series)

rows = [r for r in list(csv.reader(open(replay.OUT)))[1:] if r[0].split("-")[0] in series]
rows.sort(key=lambda r: (r[0], int(r[1])))
tick = np.array([r[0] for r in rows])
ts = np.array([int(r[1]) for r in rows])
age = np.array([float(r[2]) for r in rows])
y = np.array([float(r[3]) for r in rows])
bid = np.array([float(r[4]) for r in rows])
ask = np.array([float(r[5]) for r in rows])
X = np.array([[float(v) for v in r[6:]] for r in rows])
day = np.array([datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d") for t in ts])
days = sorted(set(day))
used = [i for i, n in enumerate(riskscore.NAMES) if n in riskscore.USED]
cush = X[:, riskscore.NAMES.index("cushion")]
zone = np.where(np.abs(cush) < 0.5, 0, np.where(np.abs(cush) < 1.5, 1, 2))
ZNAME = ["on the line (cushion under 0.5)", "fair cushion (0.5 to 1.5)", "wide cushion (over 1.5)"]
ok = (bid > 0) & (ask > 0) & (ask < 1) & (bid < 1)
at = {(a, int(b)): i for i, (a, b) in enumerate(zip(tick, ts))}
prev1 = np.array([at.get((a, int(b) - 30), -1) for a, b in zip(tick, ts)])
prev2 = np.array([at.get((a, int(b) - 60), -1) for a, b in zip(tick, ts)])

ladder_yes = np.array([0.5 * (1.0 + math.erf(c / math.sqrt(2.0))) for c in cush])  # the ladder's stand-in: the normal chance from the cushion
ask_of = {1: ask, -1: 1.0 - bid}


def cost(i: int, side: int) -> float:
    a = float(ask_of[side][i])
    return a + fee_per_contract(a)


def buyable(i: int, side: int) -> bool:
    a = float(ask_of[side][i])
    return ("yes" if side > 0 else "no") in spec.sides and spec.price[0] <= a <= spec.price[1] and not lanes.no_buy(spec, a)


def ladder_edge(i: int, side: int) -> tuple[float, float]:
    p = float(ladder_yes[i]) if side > 0 else 1.0 - float(ladder_yes[i])
    return p, p - cost(i, side)


def ladder_clears(i: int, side: int) -> bool:
    p, e = ladder_edge(i, side)
    return lanes.wants_bet(spec, p, float(ask_of[side][i]), e) and lanes.chance_ok(spec, p)


def net(i: int, side: int) -> float:
    c = cost(i, side)
    return ((y[i] if side > 0 else 1.0 - y[i]) - c) / c


def show(name: str, trips: list) -> None:
    def line(label: str, sel: list) -> None:
        if not sel:
            print(f"  {label:36s} bets     0")
            return
        u = np.array([t[0] for t in sel])
        per_day = defaultdict(float)
        for t in sel:
            per_day[t[3]] += 5 * t[0]
        print(f"  {label:36s} bets {len(u):5d} | won {int((u > 0).sum()):5d} | lost {int((u <= 0).sum()):5d} | avg cost {np.mean([t[2] for t in sel]):.2f} | net per $ {u.mean():+.4f} | on $5 stakes {5 * u.sum():+9.2f} | days ahead {sum(v > 0 for v in per_day.values())} of {len(per_day)}")

    print(f"--- {name}")
    line("all days", trips)
    for z in range(3):
        line("  " + ZNAME[z], [t for t in trips if t[1] == z])
    print("  sides: " + ", ".join(f"{k} {sum(1 for t in trips if t[4] == v)}" for k, v in (("YES", 1), ("NO", -1))))
    print()


LEVELS = (0.5, 0.25)
LAST = (("no worth check", lambda i, sd: True), ("worth at least the cost (edge over 0)", lambda i, sd: ladder_edge(i, sd)[1] > 0), ("the lane's edge bar", ladder_clears))
now_trips, base_trips = [], []
re_trips: dict[tuple[float, str], list] = {(lv, nm): [] for lv in LEVELS for nm, _ in LAST}
agree = {"same side": 0, "other side": 0, "ladder only": 0, "risk only": 0}
rng = np.random.default_rng(7)
for k in range(2, len(days)):
    tr, te = ok & np.isin(day, days[:k]), ok & (day == days[k])
    if tr.sum() < 1000 or not te.sum():
        continue
    w = replay._fit(X[tr][:, used], y[tr])
    p_yes = replay._sig(X[:, used] @ w)  # the risk score's chance that YES pays, from weights fitted on earlier days only

    def held_side(i: int, level: float) -> int:
        """The side whose risk has been at or under `level` at this mark, 30 s ago and 60 s ago (riskscore.low_risk); 0 when neither, or a look is missing."""
        if prev1[i] < 0 or prev2[i] < 0:
            return 0
        looks = [float(p_yes[j]) for j in (i, prev1[i], prev2[i])]
        for side in (1, -1):
            if riskscore.low_risk([p if side > 0 else 1.0 - p for p in looks], level):
                return side
        return 0

    done_now, done_base = {}, set()
    done_re: dict[tuple[float, str], dict] = {key: {} for key in re_trips}
    idx = np.flatnonzero(te)
    for i in idx:
        t = tick[i]
        if t not in done_now:
            for side in (1, -1):
                if buyable(i, side) and ladder_clears(i, side):
                    done_now[t] = side
                    now_trips.append((net(i, side), zone[i], cost(i, side), days[k], side))
                    break
        for lv in LEVELS:
            side = held_side(i, lv)
            if side == 0 or not buyable(i, side):
                continue
            for nm, check in LAST:
                if t not in done_re[(lv, nm)] and check(i, side):
                    done_re[(lv, nm)][t] = side
                    re_trips[(lv, nm)].append((net(i, side), zone[i], cost(i, side), days[k], side))
    for i in rng.permutation(idx):  # no signal: one random mark per market, a random side the lane may buy
        if tick[i] not in done_base:
            side = 1 if rng.random() < 0.5 else -1
            if buyable(i, side):
                done_base.add(tick[i])
                base_trips.append((net(i, side), zone[i], cost(i, side), days[k], side))
    first_re = done_re[(0.5, "no worth check")]
    for t in set(done_now) | set(first_re):
        a, b = done_now.get(t), first_re.get(t)
        agree["ladder only" if b is None else "risk only" if a is None else "same side" if a == b else "other side"] += 1

print(f"window: 2026-11-09 on; bets on ET days {', '.join(days[2:])} (the risk score's weights are fitted on every earlier day); {len(rows)} marks on {len(set(tick))} markets of {LANE}'s {len(series)} series")
print(f"the lane's own limits on every order: sides {', '.join(spec.sides)}, ask {spec.price[0]:g} to {spec.price[1]:g}, no buy from {spec.price_skip[0]:g} to {spec.price_skip[1]:g}; edge bar {spec.min_edge_room:.0%} of the room. Held to settlement, fee in, one bet a market.")
print("the ladder here is a stand-in (the normal chance from the cushion), not the live ladder's own number\n")
show("no signal: one random mark per market, a random side inside the lane's prices", base_trips)
show("NOW: the ladder picks the side (its worth clears the lane's edge bar)", now_trips)
for lv in LEVELS:
    for nm, _ in LAST:
        show(f"REORDERED: the risk score picks the side (risk at or under {lv:g}, held a minute), then the ladder last: {nm}", re_trips[(lv, nm)])
tot = sum(agree.values())
print("where the two orders put their first bet (the risk score at 0.5 with no worth check, against the ladder now), by market:")
for name, n in agree.items():
    print(f"  {name:12s} {n:5d} ({100.0 * n / max(tot, 1):.0f}%)")
print("DONE")
