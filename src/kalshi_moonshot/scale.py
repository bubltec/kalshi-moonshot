"""Stakes that follow the evidence, and how much of a lane's past counts toward it.

A fixed stake treats a lane with a proven record and one with none the same. A lane with `scale = true` bets its base stake until its own
finished bets show a return that is positive beyond doubt, and only then grows the stake and the cap on what it may have open:

  record      the return on stake of the lane's finished bets (paper and live together), their number, and their spread
  bar         enough bets and mean - scale_z * standard error > 0   (the lower bound, not the average)
  stake       scale_kelly (a quarter) of the Kelly stake that the lower bound alone justifies: f = lower bound / variance of one bet's
              return, as a share of equity. Using the lower bound makes the stake small until the evidence is strong.
  multiplier  that stake over the base stake, never under 1 and never over scale_max_mult. It multiplies the stake and the lane's
              exposure cap together, so the lane may carry a few of the larger bets at once.

Versions. Every change to what a lane does is a new version of it (lanes.version_of), and each bet is tagged with the version that made it. A new
version is usually an improvement, so it does not start from nothing, but old results do not describe it either. Each version is a vector of
the lane's settings; the further the old version's settings are from the current ones, the less its bets count:

  similarity  exp(-0.3 * the sum of the setting differences), each difference from 0 (same) to 1 (changed completely); one flipped switch
              is about 0.74, five changed settings about 0.22
  weight      the current version's bets count 1; an old version's count min(scale_old_weight, similarity); bets from before versions were
              recorded count half of scale_old_weight
  evidence    weighted mean and spread, and an effective number of bets, (sum of weights)^2 / (sum of squared weights), so old bets fade as the
              new version's own pile up, and a lane cannot ride on a record its current logic never earned

Growth only: a lane below the bar keeps its base stake, it is not cut. The hard ceilings (MAX_USD_PER_TRADE, MAX_OPEN_POSITIONS, the daily
loss limit, and the cash on the market's exchange) still apply on top; evidence moves a lane inside them, never past them.
"""

from __future__ import annotations

import ast
import json
import math
import time
from dataclasses import dataclass
from typing import Any

from . import journal
from .gate import since_ts

TTL = 120.0
LAMBDA = 0.3  # how fast similarity falls as settings differ
LEGACY = 0.5  # bets from before versions were recorded count this much of the old-version weight


@dataclass(frozen=True)
class Record:
    n: int  # finished bets counted
    mean: float  # average return on stake (weighted)
    sd: float  # spread of one bet's return on stake (weighted)
    n_eff: float = 0.0  # effective number of bets once old versions are down-weighted (0 = n)

    def lower(self, z: float) -> float:
        ne = self.n_eff or self.n
        return self.mean - z * self.sd / math.sqrt(ne) if ne > 1 else float("-inf")


@dataclass(frozen=True)
class Scale:
    mult: float
    note: str
    record: Record


def _parse(v: Any) -> Any:
    try:
        return ast.literal_eval(v) if isinstance(v, str) else v
    except (ValueError, SyntaxError):
        return v


def field_distance(a: Any, b: Any) -> float:
    """0 when two settings are the same, up to 1 when they have changed completely."""
    if a == b:
        return 0.0
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        top = max(abs(a), abs(b))
        return 1.0 if top == 0 else min(1.0, abs(a - b) / top)
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)) and a and len(a) == len(b):
        return sum(field_distance(x, y) for x, y in zip(a, b, strict=True)) / len(a)
    return 1.0


def similarity(old: dict[str, str], new: dict[str, str], lam: float = LAMBDA) -> float:
    """How alike two versions of a lane are, from 1 (the same settings) toward 0 (nothing in common)."""
    distance = sum(field_distance(_parse(old.get(k, "None")), _parse(new.get(k, "None"))) for k in set(old) | set(new))
    return math.exp(-lam * distance)


def weights_for(store: Any, lane: str, current_ver: str | None, current_settings: dict[str, str] | None, old_weight: float) -> dict[str | None, float]:
    """Weight of each version's bets for this lane, relative to the current version (which counts 1)."""
    out: dict[str | None, float] = {None: LEGACY * old_weight}
    if current_ver is None or current_settings is None:
        return {}
    for r in store.lane_versions(lane):
        if r["ver"] == current_ver:
            continue
        try:
            old = json.loads(r["settings"])
        except (TypeError, ValueError):
            continue
        out[r["ver"]] = min(old_weight, similarity(old, current_settings))
    out[current_ver] = 1.0
    return out


def record(store: Any, lanes: tuple[str, ...], since: float = 0.0, current: dict[str, tuple[str, dict[str, str]]] | None = None,
           old_weight: float = 0.5) -> Record:
    """The weighted record of these lanes' finished bets. With no `current` versions every bet counts 1 (the plain record)."""
    wmap = {lane: weights_for(store, lane, ver, st, old_weight) for lane, (ver, st) in (current or {}).items()}
    pairs: list[tuple[float, float]] = []  # (return on stake, weight)
    for t in journal.trades(store):
        if t.lane not in lanes or t.stake <= 0 or t.opened_ts < since:
            continue
        w = 1.0
        if wmap.get(t.lane):
            w = wmap[t.lane].get(t.ver, old_weight * LEGACY)  # a version we have no settings for counts like bets from before versions
        if w > 1e-6:
            pairs.append((t.pnl / t.stake, w))
    n = len(pairs)
    if n == 0:
        return Record(0, 0.0, 0.0)
    sw = sum(w for _, w in pairs)
    mean = sum(r * w for r, w in pairs) / sw
    denom = sw - sum(w * w for _, w in pairs) / sw
    sd = math.sqrt(sum(w * (r - mean) ** 2 for r, w in pairs) / denom) if denom > 1e-9 else 0.0
    n_eff = sw * sw / sum(w * w for _, w in pairs)
    return Record(n, mean, sd, n_eff)


_CACHE: dict[tuple, tuple[float, Record]] = {}


def cached_record(store: Any, lanes: tuple[str, ...], since: float = 0.0, now: float | None = None,
                  current: dict[str, tuple[str, dict[str, str]]] | None = None, old_weight: float = 0.5) -> Record:
    now = time.time() if now is None else now
    key = (id(store), lanes, since, tuple(sorted((k, v[0]) for k, v in (current or {}).items())), old_weight)
    hit = _CACHE.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]
    rec = record(store, lanes, since, current, old_weight)
    _CACHE[key] = (now, rec)
    return rec


def multiplier(rec: Record, spec: Any, equity: float, base_stake: float) -> tuple[float, str]:
    """(multiplier, why) from a lane's record. 1.0 until the lower bound clears zero."""
    lo = rec.lower(spec.scale_z)
    ne = rec.n_eff or rec.n
    counted = f"{rec.n} finished bets ({ne:.0f} effective)" if rec.n_eff and abs(ne - rec.n) > 0.5 else f"{rec.n} finished bets"
    head = f"{counted}, {rec.mean:+.1%} on stake, lower bound {lo:+.1%}" if ne > 1 else counted
    if ne < spec.scale_min_n:
        return 1.0, f"record: {head}; needs {spec.scale_min_n} before the stake can grow"
    if lo <= 0 or rec.sd <= 0 or equity <= 0 or base_stake <= 0:
        return 1.0, f"record: {head}; not above zero, so the stake stays at ${base_stake:.2f}"
    kelly_stake = spec.scale_kelly * (lo / (rec.sd**2)) * equity
    mult = min(spec.scale_max_mult, max(1.0, kelly_stake / base_stake))
    return mult, f"record: {head}; stake x{mult:.2f}"


def current_versions(s: Any, lanes: tuple[str, ...]) -> dict[str, tuple[str, dict[str, str]]]:
    from . import lanes as lanes_mod

    specs = lanes_mod.lane_specs(s)
    return {n: (lanes_mod.version_of(specs[n], s), lanes_mod.settings_of(specs[n], s)) for n in lanes if n in specs}


def for_lane(store: Any, spec: Any, equity: float, now: float | None = None, s: Any = None) -> Scale:
    """The stake multiplier for this lane now (1.0 when scaling is off or the evidence is not there)."""
    if not spec.scale:
        return Scale(1.0, "", Record(0, 0.0, 0.0))
    names = tuple(spec.scale_from) or (spec.name,)
    current = current_versions(s, names) if s is not None else None
    rec = cached_record(store, names, since_ts(spec.scale_since), now, current, spec.scale_old_weight)
    base = spec.fixed_stake or equity * spec.max_bet_pct
    mult, why = multiplier(rec, spec, equity, base)
    return Scale(mult, why, rec)
