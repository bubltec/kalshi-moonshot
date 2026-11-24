"""The wave shape: the buy step `wave_shape` (2026-11-24, PAPER), the classifier of scripts/wave_buy_shape_classifier.py (branch study commit ddd4ff0, studies/results/2026-11-24-wave-buy-shape-classifier.txt) on the in-memory spot.

Why (2026-11-24, user): the wave lanes' old buy steps (wave_top, wave_flag at M=1 and M=2) did not make money. The user labelled the wave buys by chart: the curve starts near the target (the base), swings away to an extreme, comes back near the target and repeats; the buy is NO at the second peak as it TURNS (YES at the second trough, the mirror under the target); one wide swing off the target also qualifies. The study wrote one rule against those labels: 5 of 5 qualified buys pass, 0 of 16 not qualified pass. Every setting was chosen AFTER seeing the labels and the margins are thin, so this is a lead for paper, not a result (CLAUDE.md rule 5). The settings below are the study's, not retuned.

All in the market's own variance (CLAUDE.md rule 1), from Kalshi's own spot (binaryrisk.curve_index: a coin's settlement index, a commodity's perp reference) on a 1 s grid (the last tick carried):
  u        the typical minute: root-mean-square 1 s move over the hour before now, times sqrt(60).
  rho      u60 / u, u60 the root-mean-square 60 s change over the same hour (read every 10 s): 1 for a random walk, under 1 for a spiky curve.
  W        the broad-curve window, set by the market's own noise: 60 s / rho^2, held to 30..240 s.
  broad    a median over a centred W-second window, cut at now (it reads only what the moment has).
  turns    a zig-zag on the broad curve since the open, reversal ZZ x u.
  cushion  c(x) = s (x - K) / u, s = +1 for a NO (the extreme is a peak), -1 for a YES (a trough); K the target on the curve's scale (waveflag.target).
THE RULE: the turn (the broad extreme made at most TURN_W x W s ago, at most RETR_HI of the swing given back) and a base at most CB_MAX u on the extreme's side of the target (through it is allowed), and one of
  R, repeated swing: this swing (the last confirmed opposite turn B -> the extreme E since it) has an earlier same-way swing (B2 -> E2) of REP_LO..REP_HI its size that came back at least RET of its height, the bases B and B2 at most DRIFT_MAX x the swing apart, and this swing at least H_INM of the broad range since the open;
  S, one wide swing: E is the market's extreme since the open, the swing from the opposite extreme before it is at least S_RNG of the hour's broad range and was made at S_SPEED u a minute or faster.
NO at a peak, YES at a trough. No price band (the study: the price did not separate the labels). Not read in the market's first minute.
One condition the study script did not check: the branch's extreme is past the target on its side (cE > 0: a NO's peak over the target, a YES's trough under it), as the rule was stated to this step ("YES at a dip under the target, NO at a peak over it"). Without it a curve still falling ABOVE the target reads as a YES trough (the base, a peak through the target, passes "through it is allowed"). It changes the verdict of none of the study's 982 readings (its 144 bets and the ETH 10:30 scans; four failing readings gain it as one more reason).

The hour's u, rho and range are read from the tick memory (ticks.history, filled from the tape at start and by the socket), the way waveflag.py reads its sigma10: no table is read. The broad curve is a rolling median (a sorted window, one insert and one removal a second), about 10 ms for a full hour; a reading is kept for the second it was made in, so both sides and every lane in that second share it. stdlib only.
"""

from __future__ import annotations

import bisect
import math
import threading
from dataclasses import dataclass, field
from typing import Any

from .wavetop import grid

PRE = 3600  # the hour of spot before the moment that sets u, rho and the hour's range; the path starts this long before the open
MIN_T = 60  # no reading in the market's first minute (the study did not measure one)

# The study's settings (chosen after seeing the labels; not retuned here).
ZZ = 0.7  # the zig-zag's reversal on the broad curve, in u
W_MIN, W_MAX = 30, 240  # the broad window's bounds, seconds
TURN_W = 0.3  # the turn: the broad extreme made at most this share of W ago
RETR_HI = 0.10  # ... and at most this share of the swing given back
CB_MAX = 1.0  # the base at most this many u on the extreme's side of the target (through it is allowed)
REP_LO, REP_HI = 0.75, 1.35  # R: the earlier swing over this one
RET = 0.6  # R: the earlier swing came back at least this share of its height
DRIFT_MAX = 0.4  # R: the two bases at most this share of the swing apart (no stair-step trend)
H_INM = 0.4  # R: this swing at least this share of the broad range since the open
S_RNG = 0.3  # S: the swing at least this share of the hour's broad range
S_SPEED = 0.6  # S: made at this many u a minute or faster (a wave, not a slow drift)


def median_sorted(s: list[float]) -> float:
    n = len(s)
    return 0.0 if n == 0 else (s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2]))


def broad(g: list[float], a: int, z: int, h: int, top: int) -> list[float]:
    """The broad curve at path indexes a..z: the median of g[max(0, k - h) .. min(top, k + h)] for each k (the study's centred window cut at the moment `top` and at the path's start), as a rolling sorted window."""
    lo, hi = max(0, a - h), min(top, a + h)
    win = sorted(g[lo : hi + 1])
    out: list[float] = []
    for k in range(a, z + 1):
        new_lo, new_hi = max(0, k - h), min(top, k + h)
        while hi < new_hi:
            hi += 1
            bisect.insort(win, g[hi])
        while lo < new_lo:
            del win[bisect.bisect_left(win, g[lo])]
            lo += 1
        out.append(median_sorted(win))
    return out


def zigzag(sm: list[float], rev: float) -> list[tuple[int, float, int]]:
    """Confirmed turning points on the broad curve: [(index, value, kind)], kind +1 a peak, -1 a trough, in time order (the study's zigzag)."""
    pts: list[tuple[int, float, int]] = []
    d = 0
    lo = hi = sm[0]
    klo = khi = 0
    ek, ev = 0, sm[0]
    for k in range(1, len(sm)):
        v = sm[k]
        if d == 0:
            if v < lo:
                lo, klo = v, k
            if v > hi:
                hi, khi = v, k
            if v - lo >= rev:
                pts.append((klo, lo, -1))
                d, ek, ev = 1, k, v
            elif hi - v >= rev:
                pts.append((khi, hi, 1))
                d, ek, ev = -1, k, v
            continue
        if (v - ev) * d > 0:
            ek, ev = k, v
        elif (ev - v) * d >= rev:
            pts.append((ek, ev, d))
            d, ek, ev = -d, k, v
    return pts


@dataclass
class Shape:
    """The side-free reading of one market at one second: the broad curve since the open and its turns."""

    t: int  # seconds since the open
    u: float
    rho: float
    W: int
    rng_hour: float  # the broad curve's range over the hour, in price
    sm: list[float]  # the broad curve, index 0 = the open
    pts: list[tuple[int, float, int]] = field(default_factory=list)


@dataclass
class Verdict:
    side: str
    ok: bool
    branch: str  # "R", "S", "RS" or ""
    f: dict[str, Any]  # the readings
    fails_r: list[str]
    fails_s: list[str]


def shape(g: list[float | None], t: int) -> tuple[Shape | None, str]:
    """The reading at `t` seconds after the open, from the spot `g` on a 1 s grid that starts PRE seconds before the open and ends at the moment (index PRE + t). (Shape, "") or (None, why not)."""
    if t < MIN_T:
        return None, "the market's first minute: no wave shape is read yet"
    ib = PRE + t
    if len(g) < ib + 1:
        return None, "the spot's path does not reach the moment"
    g = g[: ib + 1]
    if None in g[ib - PRE :]:
        return None, "the spot's history does not cover the hour before the moment"
    hour: list[float] = g[ib - PRE : ib + 1]  # type: ignore[assignment]
    d1 = sum((hour[k] - hour[k - 1]) ** 2 for k in range(1, len(hour))) / (len(hour) - 1)
    u = math.sqrt(d1) * math.sqrt(60)
    if u <= 0:
        return None, "the spot did not move in the hour, so its typical minute cannot be read"
    d60 = [hour[k] - hour[k - 60] for k in range(60, len(hour), 10)]
    u60 = math.sqrt(sum(x * x for x in d60) / len(d60))
    rho = u60 / u
    W = int(min(W_MAX, max(W_MIN, 60.0 / max(rho, 1e-6) ** 2)))
    sm_hour = broad(g, ib - PRE, ib, W // 2, ib)  # type: ignore[arg-type]
    sm = sm_hour[PRE - t :]
    return Shape(t, u, rho, W, max(sm_hour) - min(sm_hour), sm, zigzag(sm, ZZ * u)), ""


def judge(sh: Shape, side: str, K: float) -> Verdict:
    """The study's measure and judge for one side: the readings and the fail reasons of each branch; the side qualifies when either branch has none."""
    s = 1 if side == "no" else -1
    sm, u, pts = sh.sm, sh.u, sh.pts
    c = lambda x: s * (x - K) / u  # noqa: E731
    now = sm[-1]
    rng_inm = max(sm) - min(sm)
    f: dict[str, Any] = {"t": sh.t, "u": u, "rho": sh.rho, "W": sh.W, "rng_u": sh.rng_hour / u, "inm_u": rng_inm / u, "c_now": c(now), "rep": None, "ret2": None, "drift": None}
    # S: the in-market extreme on the side's side and the opposite in-market extreme before it
    vX = max(sm) if s == 1 else min(sm)
    kX = sm.index(vX)
    pre = sm[: kX + 1]
    vO = min(pre) if s == 1 else max(pre)
    kO = pre.index(vO)
    HS = s * (vX - vO)
    f.update(S_cB=c(vO), S_cE=c(vX), S_H_u=HS / u, S_rng=HS / sh.rng_hour if sh.rng_hour else 0.0, S_speed=HS / u / max(kX - kO, 1) * 60.0, S_since=len(sm) - 1 - kX, S_retr=s * (vX - now) / HS if HS > 0 else 1.0)
    # R: the extreme E since the last confirmed opposite turn B
    bases = [p for p in pts if p[2] == -s]
    if bases:
        kB, vB, _ = bases[-1]
        seg = sm[kB:]
        vE = max(seg) if s == 1 else min(seg)
        kE = kB + seg.index(vE)
        H = s * (vE - vB)
        f.update(cB=c(vB), cE=c(vE), H_u=H / u, H_inm=H / rng_inm if rng_inm else 0.0, since=len(sm) - 1 - kE, retr=s * (vE - now) / H if H > 0 else 1.0)
        prev = [p for p in pts if p[0] < kB and p[2] == s]
        if prev:  # the earlier swing the same way: a confirmed extreme E2 before B, with its own base B2 before it
            kE2, vE2, _ = prev[-1]
            b2 = [p for p in pts if p[0] < kE2 and p[2] == -s]
            vB2 = b2[-1][1] if b2 else (min(sm[: kE2 + 1]) if s == 1 else max(sm[: kE2 + 1]))
            H2 = s * (vE2 - vB2)
            f.update(rep=H2 / H if H > 0 else 0.0, ret2=s * (vE2 - vB) / H2 if H2 > 0 else 0.0, drift=abs(vB - vB2) / H if H > 0 else 9.9, cB2=c(vB2))

    def turn(since: int, retr: float) -> bool:
        return since <= TURN_W * sh.W and 0.0 <= retr <= RETR_HI

    fr: list[str] = []
    if not bases:
        fr.append("no confirmed base")
    else:
        if f["rep"] is None:
            fr.append("no earlier swing")
        else:
            if not REP_LO <= f["rep"] <= REP_HI:
                fr.append("swings not comparable")
            if f["ret2"] < RET:
                fr.append("earlier swing did not come back")
            if f["drift"] > DRIFT_MAX:
                fr.append("bases drift")
        if f["H_inm"] < H_INM:
            fr.append("small vs the market")
        if not turn(f["since"], f["retr"]):
            fr.append("not at the turn")
        if f["cB"] > CB_MAX:
            fr.append("base far from the target")
        if f["cE"] <= 0:
            fr.append("extreme not past the target")
    fs: list[str] = []
    if f["S_rng"] < S_RNG:
        fs.append("small vs the hour")
    if f["S_speed"] < S_SPEED:
        fs.append("slow drift")
    if f["S_cB"] > CB_MAX:
        fs.append("base far from the target")
    if not turn(f["S_since"], f["S_retr"]):
        fs.append("not at the turn")
    if f["S_cE"] <= 0:
        fs.append("extreme not past the target")
    branch = ("R" if not fr else "") + ("S" if not fs else "")
    return Verdict(side, bool(branch), branch, f, fr, fs)


def describe(v: Verdict) -> str:
    """The readings in one clause for the decision's note: branch, swing, W, rho, base distance, turn age."""
    f = v.f
    head = f"W {f['W']}s, rho {f['rho']:.2f}, typical minute {f['u']:.6g}, {f['t'] / 60:.1f} min in"
    if "cB" in f:
        r = f"R: swing {f['H_u']:.1f}u ({f['H_inm']:.2f} of the range since the open), base {f['cB']:+.1f}u, extreme {f['cE']:+.1f}u, turn {f['since']}s ago with {f['retr']:.2f} given back"
        if f["rep"] is not None:
            r += f", earlier swing {f['rep']:.2f}x that came back {f['ret2']:.2f}, bases {f['drift']:.2f} of the swing apart"
    else:
        r = "R: no confirmed base"
    sr = f"S: swing {f['S_H_u']:.1f}u ({f['S_rng']:.2f} of the hour's range) at {f['S_speed']:.2f}u/min, base {f['S_cB']:+.1f}u, extreme {f['S_cE']:+.1f}u, turn {f['S_since']}s ago with {f['S_retr']:.2f} given back"
    if v.ok:
        return f"wave shape {v.branch} ({'a repeated swing' if v.branch.startswith('R') else 'one wide swing'}): {head}; {r}; {sr}; buys {v.side.upper()}"
    return f"no wave shape for {v.side.upper()} [R: {'; '.join(v.fails_r)} | S: {'; '.join(v.fails_s)}]: {head}; {r}; {sr}"


_memo: dict[tuple[Any, ...], tuple[Shape | None, str]] = {}
_memo_lock = threading.Lock()


def read_index(index: str, opened: float, now: float) -> tuple[Shape | None, str]:
    """`shape` on the tick memory, kept for the second it was read in (both sides and every lane ask the same question)."""
    from . import ticks as ticks_mod

    key = (index, int(opened), int(now))
    with _memo_lock:
        if key in _memo:
            return _memo[key]
    a = int(opened) - PRE
    got = ticks_mod.history.window(index, a, now)
    if not got or not got[0]:
        out: tuple[Shape | None, str] = (None, f"no tick history in memory for {index} back to an hour before the open")
    else:
        T, V = got
        if T[0] > a:  # the path's first second carries the last tick before it, however old (a commodity's session break), as the study's pull did
            prev = ticks_mod.history.last_at_or_before(index, a)
            if prev is not None:
                T, V = [prev[0], *T], [prev[1], *V]
        t = int(now) - int(opened)
        out = shape(grid(T, V, a, int(now) + 1), t)
    with _memo_lock:
        if len(_memo) > 400:
            _memo.clear()
        _memo[key] = out
    return out


def admits(s: Any, shot: Any, now: float, strike: float | None) -> tuple[bool, str]:
    """The buy step's answer for one side: (buy?, the sentence for the decision's note). `strike` is the market's own (Kalshi's) strike; waveflag.target puts the target on the curve's scale (a commodity's perp value at the open). Memory only."""
    from . import binaryrisk, waveflag

    series = shot.series or shot.ticker.split("-")[0]
    index = binaryrisk.curve_index(series)
    if not index:
        return False, f"no Kalshi index or perp reference is read for {series}"
    opened = round((now + shot.hours_to_close * 3600.0) / 900.0) * 900.0 - 900.0
    K, why = waveflag.target(series, index, opened, strike)
    if K is None:
        return False, why
    sh, why = read_index(index, opened, now)
    if sh is None:
        return False, f"no wave shape: {why}"
    v = judge(sh, shot.side, K)
    return v.ok, describe(v) + f" (target {K:.6g})"
