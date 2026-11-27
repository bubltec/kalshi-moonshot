"""Wave, trend and spike read from the spot's last half hour as an oscillator (2026-11-27; user: "First we need to get our wave, spike and trend logic down. We should be using oscillation mathematics and differential equations to calculate wave patterns.
And every wave is 'seen' by reading the spot history back about 30 mins. This is where you can see: how many waves, how frequent the waves, and where the waves are oscillating around the current target").

THE MODEL. Over the look-back the spot is taken as three things added together:
  a TREND     a straight line through the window (least squares): its slope, and how far the line travels over the window against the wobble around it;
  a WAVE      what is left around the line, read as a damped oscillator driven by noise:   x'' + 2*g*x' + w0^2 * x = noise   (x the spot less the line; w0 the natural frequency, g the damping).
              w0 and g are fitted by least squares of the measured acceleration on the measured position and velocity (the differential equation itself, on the smoothed path). From them: the period 2*pi / sqrt(w0^2 - g^2),
              the damping ratio g / w0 (under 1 = it swings; near 0 = it keeps swinging; 1 or more = it does not come back as a wave), and how much of the acceleration the equation explains (fit).
              Beside the fit, the waves are COUNTED on the path itself: crossings of the line with a dead band, so "how many" and "how frequent" do not rest on the fit alone;
  SPIKES      single 10 s moves far outside the window's own ordinary moves (over SPIKE_Z robust sigmas of the 10 s changes): how many, and what share of all the movement they carry.
WHERE THE TARGET SITS: the line's value now is the centre the waves swing around; the target's distance from it is given in wave amplitudes (0 = the waves swing across the target; beyond 1 = the target is outside the swing).
WHERE THE SPOT IS IN ITS SWING: the position now in amplitudes and the phase from the position and the velocity (0 = at the top, turning down; 0.5 = at the bottom, turning up; counted as a share of a full swing).

Everything is in the market's own units (its amplitude, its period, its ordinary 10 s move). No asset name, no fixed cents. stdlib only. A READING: it says what the half hour looks like and decides nothing."""
from __future__ import annotations

import math
import statistics
from typing import Any

GRID = 10  # seconds between samples
LOOK = 1800  # seconds read back: the user's "about 30 mins"
SMOOTH = 3  # samples each side in the moving average the derivatives are taken on (a 70 s window): the equation is fitted on the path's shape, not on its tick noise
SPIKE_Z = 5.0  # a 10 s move this many robust sigmas of the window's own 10 s moves is a spike (the lane's spike trail uses 5 of its own units too)
DEAD = 0.25  # the dead band of the wave count, in amplitudes: a crossing counts once the path is this far the other side of the line


def _line(y: list[float]) -> tuple[float, float]:
    n = len(y)
    xm, ym = (n - 1) / 2.0, statistics.fmean(y)
    sxx = sum((i - xm) ** 2 for i in range(n))
    b = sum((i - xm) * (v - ym) for i, v in enumerate(y)) / sxx
    return ym - b * xm, b


def _smooth(x: list[float], h: int) -> list[float]:
    n = len(x)
    out = []
    for i in range(n):
        lo, hi = max(0, i - h), min(n - 1, i + h)
        out.append(sum(x[lo : hi + 1]) / (hi - lo + 1))
    return out


def read(y: list[float], target: float | None = None) -> dict[str, Any] | None:
    """The oscillator reading of a path sampled every GRID seconds, oldest first (about LOOK seconds of it). None when there is too little path or it does not move."""
    n = len(y)
    if n < 60:
        return None
    a, b = _line(y)
    x = [v - (a + b * i) for i, v in enumerate(y)]  # the path around its line
    sd = statistics.pstdev(x)
    if sd <= 0:
        return None
    amp = sd * math.sqrt(2.0)  # the amplitude of a sine with this spread
    d = [y[i] - y[i - 1] for i in range(1, n)]
    med = statistics.median(d)
    mad = statistics.median(abs(v - med) for v in d) * 1.4826
    step = mad if mad > 0 else (statistics.pstdev(d) or 1e-12)  # the window's ordinary 10 s move, robust to its own spikes
    spikes = [i for i, v in enumerate(d) if abs(v - med) > SPIKE_Z * step]
    tot = sum(v * v for v in d) or 1e-12
    spike_share = sum(d[i] * d[i] for i in spikes) / tot
    # the differential equation, on the smoothed path: acceleration = -w0^2 * position - 2g * velocity
    s = _smooth(x, SMOOTH)
    pos, vel, acc = [], [], []
    for i in range(1, n - 1):
        pos.append(s[i])
        vel.append((s[i + 1] - s[i - 1]) / (2.0 * GRID))
        acc.append((s[i + 1] - 2.0 * s[i] + s[i - 1]) / (GRID * GRID))
    spp, svv, spv = sum(p * p for p in pos), sum(v * v for v in vel), sum(p * v for p, v in zip(pos, vel, strict=True))
    spa, sva = sum(p * q for p, q in zip(pos, acc, strict=True)), sum(v * q for v, q in zip(vel, acc, strict=True))
    det = spp * svv - spv * spv
    w0sq = g = None
    fit = 0.0
    if det > 0:
        c1 = (spa * svv - sva * spv) / det  # acc = c1 * pos + c2 * vel
        c2 = (sva * spp - spa * spv) / det
        w0sq, g = -c1, -c2 / 2.0
        saa = sum(q * q for q in acc) or 1e-30
        res = sum((q - c1 * p - c2 * v) ** 2 for p, v, q in zip(pos, vel, acc, strict=True))
        fit = max(0.0, 1.0 - res / saa)
    period = zeta = None
    if w0sq is not None and g is not None and w0sq > 0:
        w0 = math.sqrt(w0sq)
        zeta = g / w0
        wd2 = w0sq - g * g
        period = 2.0 * math.pi / math.sqrt(wd2) if wd2 > 0 else None  # None: it does not swing (critically or over damped)
    # the waves counted on the path: crossings of the line with a dead band
    side, crossings, last_cross = 0, 0, None
    gaps: list[int] = []
    for i, v in enumerate(s):
        now = 1 if v > DEAD * amp else -1 if v < -DEAD * amp else 0
        if now and now != side:
            if side:
                crossings += 1
                if last_cross is not None:
                    gaps.append(i - last_cross)
                last_cross = i
            elif last_cross is None:
                last_cross = i
            side = now
    waves = crossings / 2.0
    counted_period = 2.0 * statistics.median(gaps) * GRID if gaps else None
    span_min = (n - 1) * GRID / 60.0
    drift = abs(b) * (n - 1)  # how far the line travels over the window
    x_now, v_now = s[-1], (s[-1] - s[-3]) / (2.0 * GRID) if n >= 3 else 0.0
    use = period or counted_period
    phase = None
    if use:
        w = 2.0 * math.pi / use
        phase = (math.atan2(-v_now / w, x_now) / (2.0 * math.pi)) % 1.0  # 0 at the top turning down, 0.25 falling through the line, 0.5 at the bottom turning up, 0.75 rising through the line
    centre = a + b * (n - 1)
    out = {
        "span_min": round(span_min, 1),
        "trend_per_min_amp": round(b * (60.0 / GRID) / amp, 4),  # the line's slope, in amplitudes a minute (signed)
        "trend_x": round(drift / amp, 2),  # the line's travel over the window, in amplitudes: under about 1 the half hour is a wave around a level; well over, it is a trend with a wobble on it
        "waves": waves, "waves_per_15": round(waves * 15.0 / span_min, 2),
        "period_s": None if period is None else round(period, 0), "counted_period_s": None if counted_period is None else round(counted_period, 0),
        "damping": None if zeta is None else round(zeta, 3), "fit": round(fit, 3),
        "amp": amp, "pos_now_amp": round(x_now / amp, 2), "phase": None if phase is None else round(phase, 2),
        "spikes": len(spikes), "spike_share": round(spike_share, 3), "last_spike_s_ago": None if not spikes else (n - 2 - spikes[-1]) * GRID,
        "centre": centre,
    }
    if target is not None:
        out["target_amp"] = round((target - centre) / amp, 2)  # where the target sits against the swing: 0 = at its centre, +1 = at the top of the swing, beyond 1 = outside it
        out["spot_to_target_amp"] = round((y[-1] - target) / amp, 2)
    return out


def kind(r: dict[str, Any]) -> str:
    """A first naming of the half hour from the reading, for a note (not a rule): 'wave', 'trend', 'spike', or two of them joined when both show."""
    names = []
    if r["spike_share"] >= 0.25:
        names.append("spike")
    if r["trend_x"] >= 2.0:
        names.append("trend")
    if r["waves"] >= 2.0 and (r["trend_x"] < 2.0 or r["fit"] >= 0.3):  # waves around a level, or a clear wave riding a trend
        names.append("wave")
    return "+".join(names) or "flat"
