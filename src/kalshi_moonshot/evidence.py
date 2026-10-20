"""Judge a buy/sell idea against history the same way every time.

An idea is a rule: which contract to buy (YES, NO or either), at what price, in which minutes of the
window, triggered by what (a price slope, a dip, nothing at all), and how to leave (hold to the end,
sell when the slope turns, trail the peak, a resting sell at a multiple, a stop). Rules live in a
TOML file (see rules.toml); any list-valued setting inside `entry` or `exit` is a grid, and every
combination is tried.

Each rule is then scored like this:

  * Windows are sorted by time. The earliest `split` share is where settings are tuned; the later share
    is held out and only looked at to judge the choice made on the earlier one.
  * A baseline runs the same side, price band, minutes and exit with no trigger at all. What a rule adds
    over its baseline is the value of its signal; the baseline alone shows what trading costs.
  * Held-out return comes with a 90% interval from resampling whole 15-minute boundaries (markets
    that close together move together, so they are resampled together).
  * The number of settings tried is reported, because the best of many will look good by chance.

Nothing here places an order. Fees on both legs and the bid-ask spread are always counted: entries
cross the spread at the ask, exits at the bid, and a resting sell fills at its target price.
"""

from __future__ import annotations

import itertools
import random
import tomllib
from dataclasses import dataclass, field
from typing import Any

from .store import Store
from .strategy import fee_per_contract

ENTRY_KINDS = {"slope", "dip", "always"}
EXIT_KINDS = {"hold", "slope_turn", "trail", "target", "stop", "time"}
END = 14  # last minute an exit can happen at before settlement


@dataclass(frozen=True)
class Window:
    series: str
    close_ts: int
    result: str
    path: dict[int, tuple[float, float, float, float]]  # minute -> yes bid, ask, best bid, cheapest ask


def load_windows(store: Store, series: list[str] | None = None) -> list[Window]:
    out: list[Window] = []
    for w in store.db.execute("SELECT ticker, series, close_ts, result FROM windows ORDER BY close_ts"):
        if series and w["series"] not in series:
            continue
        path: dict[int, tuple[float, float, float, float]] = {}
        for r in store.db.execute(
            "SELECT end_ts, bid_c, ask_c, bid_hi, ask_lo FROM candles WHERE ticker=? ORDER BY end_ts", (w["ticker"],)
        ):
            k = 15 - (w["close_ts"] - r["end_ts"]) // 60
            if 1 <= k <= 15 and r["bid_c"] is not None and r["ask_c"] is not None:
                path[k] = (r["bid_c"], r["ask_c"],
                           r["bid_hi"] if r["bid_hi"] is not None else r["bid_c"],
                           r["ask_lo"] if r["ask_lo"] is not None else r["ask_c"])
        if len(path) >= 12:
            out.append(Window(w["series"], int(w["close_ts"]), w["result"], path))
    return out


@dataclass(frozen=True)
class Side:
    bid: dict[int, float]
    ask: dict[int, float]
    best_bid: dict[int, float]
    mid: dict[int, float]


def side_view(w: Window, side: str) -> Side:
    if side == "yes":
        bid = {k: v[0] for k, v in w.path.items()}
        ask = {k: v[1] for k, v in w.path.items()}
        best = {k: v[2] for k, v in w.path.items()}
    else:  # NO: its bid is one minus the YES ask; its best bid one minus the cheapest YES ask
        bid = {k: 1 - v[1] for k, v in w.path.items()}
        ask = {k: 1 - v[0] for k, v in w.path.items()}
        best = {k: 1 - v[3] for k, v in w.path.items()}
    return Side(bid, ask, best, {k: (bid[k] + ask[k]) / 2 for k in w.path})


# ---------------------------------------------------------------- one rule, one setting


@dataclass(frozen=True)
class Setting:
    name: str
    sides: tuple[str, ...]
    price: tuple[float, float]
    minutes: tuple[int, int]
    max_spread: float
    entry: tuple[tuple[str, Any], ...]
    exit: tuple[tuple[str, Any], ...]

    @property
    def e(self) -> dict[str, Any]:
        return dict(self.entry)

    @property
    def x(self) -> dict[str, Any]:
        return dict(self.exit)

    def label(self) -> str:
        parts = [f"{k}={v}" for k, v in self.entry if k != "kind"] + [f"exit:{self.x['kind']}"] + [
            f"{k}={v}" for k, v in self.exit if k != "kind"]
        return f"{self.e['kind']} " + " ".join(parts)

    def baseline(self) -> Setting:
        return Setting(self.name, self.sides, self.price, self.minutes, self.max_spread, (("kind", "always"),), self.exit)


@dataclass(frozen=True)
class Trade:
    cost: float
    got: float
    close_ts: int
    series: str


def _entry_minute(sv: Side, e: dict[str, Any], lo: int, hi: int, band: tuple[float, float], max_spread: float) -> int | None:
    kind = e["kind"]
    window = int(e.get("window", 3))
    high = 0.0
    for k in range(1, hi + 1):
        if k in sv.mid:
            high = max(high, sv.mid[k])
        if k < lo or k not in sv.ask or not band[0] <= sv.ask[k] <= band[1] or sv.ask[k] - sv.bid[k] > max_spread:
            continue
        if kind == "always":
            return k
        if kind == "slope":
            if (k - window) not in sv.mid:
                continue
            slope = (sv.mid[k] - sv.mid[k - window]) / window
            need = float(e.get("min", 0.02))
            if (slope >= need) if e.get("direction", "follow") == "follow" else (slope <= -need):
                return k
        if kind == "dip" and sv.mid[k] <= high - float(e.get("drop", 0.05)):
            return k
    return None


def _exit(sv: Side, k: int, x: dict[str, Any], result_wins: bool) -> float:
    """What one contract bought at the ask of minute k returns. Exits leave at the bid and pay a fee."""
    entry = sv.ask[k]
    kind = x["kind"]
    settle = 1.0 if result_wins else 0.0
    peak = sv.mid[k]
    downs = 0
    for j in range(k + 1, END + 1):
        if j not in sv.mid:
            continue
        peak = max(peak, sv.mid[j])
        sell = None
        if kind == "slope_turn":
            downs = downs + 1 if sv.mid[j] < sv.mid.get(j - 1, sv.mid[j]) else 0
            if downs >= int(x.get("ticks", 1)):
                sell = sv.bid[j]
        elif (kind == "trail" and sv.mid[j] <= peak - float(x.get("drop", 0.04))) or (kind == "stop" and sv.bid[j] <= entry - float(x.get("drop", 0.05))) or (kind == "time" and j >= int(x.get("minute", 13))):
            sell = sv.bid[j]
        elif kind == "target":
            target = entry * float(x["multiple"]) if "multiple" in x else entry + float(x.get("cents", 0.1))
            if target < 1.0 and sv.best_bid[j] >= target:
                return target - fee_per_contract(target)  # a resting sell fills at its price
        if sell is not None:
            return sell - fee_per_contract(max(sell, 0.0))
    return settle


def run_setting(windows: list[Window], st: Setting) -> list[Trade]:
    out: list[Trade] = []
    for w in windows:
        for side in st.sides:
            sv = side_view(w, side)
            k = _entry_minute(sv, st.e, st.minutes[0], st.minutes[1], st.price, st.max_spread)
            if k is None:
                continue
            cost = sv.ask[k] + fee_per_contract(sv.ask[k])
            out.append(Trade(cost, _exit(sv, k, st.x, w.result == side), w.close_ts, w.series))
    return out


# ---------------------------------------------------------------- statistics


@dataclass(frozen=True)
class Stats:
    n: int
    roi: float
    win: float
    lo: float  # 90% interval from resampling boundaries
    hi: float

    @property
    def ok(self) -> bool:
        return self.n > 0


def _roi(trades: list[Trade]) -> float:
    c = sum(t.cost for t in trades)
    return (sum(t.got for t in trades) - c) / c if c else float("nan")


def stats(trades: list[Trade], resamples: int = 300, seed: int = 4) -> Stats:
    if not trades:
        return Stats(0, float("nan"), float("nan"), float("nan"), float("nan"))
    groups: dict[int, list[Trade]] = {}
    for t in trades:
        groups.setdefault(t.close_ts, []).append(t)
    keys = list(groups)
    rng = random.Random(seed)
    boots = []
    for _ in range(resamples):
        pick = [t for k in (rng.choice(keys) for _ in keys) for t in groups[k]]
        boots.append(_roi(pick))
    boots.sort()
    return Stats(len(trades), _roi(trades), sum(1 for t in trades if t.got > t.cost) / len(trades),
                 boots[int(0.05 * resamples)], boots[int(0.95 * resamples) - 1])


# ---------------------------------------------------------------- rule files


@dataclass
class Rule:
    name: str
    about: str
    settings: list[Setting] = field(default_factory=list)


def _expand(table: dict[str, Any]) -> list[tuple[tuple[str, Any], ...]]:
    keys = sorted(table)
    options = [table[k] if isinstance(table[k], list) else [table[k]] for k in keys]
    return [tuple(zip(keys, combo, strict=True)) for combo in itertools.product(*options)]


def parse_rules(text: str) -> list[Rule]:
    raw = tomllib.loads(text)
    out: list[Rule] = []
    for name, r in (raw.get("rule") or {}).items():
        entry, exit_ = dict(r.get("entry") or {"kind": "always"}), dict(r.get("exit") or {"kind": "hold"})
        if entry.get("kind") not in ENTRY_KINDS:
            raise ValueError(f"rule {name!r}: entry kind must be one of {sorted(ENTRY_KINDS)}")
        if exit_.get("kind") not in EXIT_KINDS:
            raise ValueError(f"rule {name!r}: exit kind must be one of {sorted(EXIT_KINDS)}")
        side = r.get("side", "either")
        if side not in {"yes", "no", "either"}:
            raise ValueError(f"rule {name!r}: side must be yes, no or either")
        unknown = set(r) - {"about", "side", "price", "minutes", "max_spread", "series", "entry", "exit"}
        if unknown:
            raise ValueError(f"rule {name!r}: unknown setting(s) {sorted(unknown)}")
        rule = Rule(name, r.get("about", ""))
        for e in _expand(entry):
            for x in _expand(exit_):
                rule.settings.append(Setting(
                    name, ("yes", "no") if side == "either" else (side,), tuple(r.get("price", (0.15, 0.85))),
                    tuple(r.get("minutes", (3, 10))), float(r.get("max_spread", 0.05)), e, x,
                ))
        out.append(rule)
    return out


def split_windows(windows: list[Window], share: float) -> tuple[list[Window], list[Window]]:
    cut = int(len(windows) * share)
    return windows[:cut], windows[cut:]


@dataclass(frozen=True)
class Result:
    setting: Setting
    train: Stats
    test: Stats
    base_test: Stats


@dataclass(frozen=True)
class Verdict:
    rule: str
    tried: int
    chosen: Result | None
    label: str
    reason: str
    positive_held_out: int
    base_train: Stats
    base_test: Stats
    results: list[Result]


def evaluate(rule: Rule, train: list[Window], test: list[Window], min_trades: int = 60) -> Verdict:
    results: list[Result] = []
    base_cache: dict[Setting, tuple[Stats, Stats]] = {}
    for st in rule.settings:
        b = st.baseline()
        if b not in base_cache:
            base_cache[b] = (stats(run_setting(train, b)), stats(run_setting(test, b)))
        a, t = stats(run_setting(train, st)), stats(run_setting(test, st))
        if a.n >= min_trades and t.n >= min_trades // 2:
            results.append(Result(st, a, t, base_cache[b][1]))
    first = next(iter(base_cache.values()), (Stats(0, float("nan"), float("nan"), float("nan"), float("nan")),) * 2)
    if not results:
        return Verdict(rule.name, len(rule.settings), None, "NOT ENOUGH DATA", "no setting made enough trades", 0, first[0], first[1], [])
    results.sort(key=lambda r: r.train.roi, reverse=True)
    top = results[0]  # the setting you would actually pick: best on the tuning windows
    pos = sum(1 for r in results if r.test.roi > 0)
    gain = top.test.roi - top.base_test.roi
    if top.test.lo > 0 and top.train.roi > 0:
        label, why = "SUPPORTED", "positive on both, and the held-out interval is entirely above zero"
    elif top.test.roi > 0 and top.train.roi > 0:
        label, why = "PROMISING", "positive on both, but the held-out interval still includes zero: needs more windows"
    elif top.test.roi > top.base_test.roi:
        label, why = "NOT PROFITABLE", f"it beats its no-signal baseline by {gain:+.1%} but still loses after costs"
    else:
        label, why = "NOT SUPPORTED", "no better than trading without the signal"
    return Verdict(rule.name, len(rule.settings), top, label, why, pos, first[0], first[1], results)
