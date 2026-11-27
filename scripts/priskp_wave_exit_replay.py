"""Does taking a profit when the swing the bet rides is done beat the bid-fall rule and what the bets actually made? (2026-11-26; user, on the two-wobble fix: "This is that rigid logic that gets us in trouble because it's a patch for a single market behavior ... We need to apply a logical algorithm for this that apples to any situation so it takes early profit in stride without minimizing it"; on the wave exit as described: "That could work".)
QUESTION (one, on the same bets): every live PriskP bet since the per-tick entry change (2026-11-25 15:57 ET), walked second by second from its buy with the one rule (fallrule.py) twice: BID FALL, its profit side as deployed (a fall of the bid from its high, in wobbles); WAVE, its profit side replaced by the entry's own reading of the curve (wavefacts.leg for the bet's side, the reading the lane buys on): a bet that is ahead after both fees is sold when the swing it rides has turned or runs against it, or when the leg is at its extreme (it has run a usual leg's length or travelled a usual wave's distance), and is held while it is inside a young leg. The loss side, the cushion hold and the top are the same in both. What does each bet make against what it actually made?
HOW A BET IS SCORED: the first sale is the result, at that second's bid less the sale fee 0.07 p (1 - p); with no sale the bet settles on the market's result. The buys rested (no buy fee) on both sides of the comparison. No buy-back is modelled: a bet sold is gone, where the live lane may buy the market again.
HONEST LIMITS: the wave reading is taken every 3 s from the settlement index's taped ticks (the live lane reads the same index); the bid is the tape's last quote of each second, no fill modelled; the spot cushion is the index's 60 s average against the strike. The wave reading's constants (a swing of 0.5 typical minutes, a give-back of 0.2, the extreme at one usual leg) are the entry's own, set before this; nothing here was fitted. A day and a half of bets: a lead, scored forward.
SHAPES: thirds of the bid's wobble at the buy, and the leg's class at the buy; no asset.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_wave_exit_replay.py BETS.json.gz INDEX.json.gz   (scripts/priskp_exit_tape_pull.py and scripts/priskp_index_tape_pull.py)"""
import datetime
import gzip
import json
import math
import re
import sys

from kalshi_moonshot import fallrule, ticks, wavefacts
from kalshi_moonshot.kalshiws import index_for_series

ET = datetime.timezone(datetime.timedelta(hours=-5))
TICK = 0.01
EVERY = 3  # seconds between wave readings
FLAG = ("KXDOGE15M-26NOV262330", "KXBTC15M-26NOV261700", "KXZEC15M-26NOV262230", "KXHYPE15M-26NOV262245", "KXXRP15M-26NOV262245", "KXBTC15M-26NOV262230", "KXDOGE15M-26NOV262230", "KXNEAR15M-26NOV262100", "KXNEAR15M-26NOV262145", "KXSOL15M-26NOV262030", "KXHYPE15M-26NOV262145", "KXDOGE15M-26NOV261145", "KXZEC15M-26NOV261545", "KXDOGE15M-26NOV251945")


def fee(p: float) -> float:
    return 0.07 * p * (1.0 - p)


def hm(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M:%S")


bets = json.load(gzip.open(sys.argv[1]))
ticks.history.keep = 1e12
ticks.history.slack = 1e12
for ix, (T, V) in json.load(gzip.open(sys.argv[2])).items():
    for t, v in zip(T, V, strict=True):
        ticks.history.note(ix, float(t), float(v))


def walk(b: dict, mode: str) -> dict | None:
    close, side, entry = b["close"], b["side"], b["entry"]
    bid, ask, idx, strike = b["bid"], b["ask"], b["idx"], b["strike"]
    if len(bid) != 900 or len(idx) != 1520 or not strike or b["result"] not in ("yes", "no"):
        return None
    index = index_for_series(re.match(r"(KX[A-Z0-9]+15M)", b["ticker"]).group(1))
    opened = close - 900
    q0, i0 = close - 900, close - 1520
    sb = [None if (x is None or y is None) else (x if side == "yes" else 1.0 - y) for x, y in zip(bid, ask, strict=True)]
    mid = [None if (x is None or y is None) else (x + y) / 2.0 for x, y in zip(bid, ask, strict=True)]
    sq, cnt = [0.0], [0]
    for a, c in zip(mid, mid[1:], strict=False):
        ok = a is not None and c is not None
        sq.append(sq[-1] + ((c - a) ** 2 if ok else 0.0))
        cnt.append(cnt[-1] + (1 if ok else 0))
    isq, icnt, isum, iok = [0.0], [0], [0.0], [0]
    for k, v in enumerate(idx):
        p = idx[k - 1] if k else None
        ok = v is not None and p is not None
        isq.append(isq[-1] + ((v - p) ** 2 if ok else 0.0))
        icnt.append(icnt[-1] + (1 if ok else 0))
        isum.append(isum[-1] + (v or 0.0))
        iok.append(iok[-1] + (1 if v is not None else 0))
    won = 1.0 if b["result"] == side else 0.0
    actual_sale = b["exit_price"] if b["exit_ts"] else None
    out = {"actual": (actual_sale - fee(actual_sale) if actual_sale is not None else won) - entry, "new": won - entry, "kind": "settled", "at": None, "px": None, "wob": None, "cls0": None, "why": ""}
    st = fallrule.State()
    wave, wave_at, legd = None, -99, None
    for k in range(max(int(math.ceil(b["ts"])) + 1 - (q0 + 1), 1), 899):
        now = q0 + 1 + k
        x = sb[k]
        if x is None or x <= 0:
            continue
        left = close - now
        unit = math.sqrt(60.0 * (sq[k] - sq[0]) / cnt[k]) if (k >= 90 and cnt[k] >= 60 and sq[k] > 0) else None
        wob = max(TICK, unit * math.sqrt(10.0 / 60.0)) if unit else TICK
        j = now - (i0 + 1)
        cushion = sunit = None
        if j >= 600 and iok[j + 1] - iok[j - 59] == 60 and icnt[j + 1] - icnt[j - 599] >= 60:
            sunit = math.sqrt(60.0 * (isq[j + 1] - isq[j - 599]) / (icnt[j + 1] - icnt[j - 599]))
            cushion = ((isum[j + 1] - isum[j - 59]) / 60.0 - strike) * (1.0 if side == "yes" else -1.0)
        if mode == "wave" and now - wave_at >= EVERY:
            wave_at = now
            legd = wavefacts.leg(index, float(opened), float(now), strike, side, float(left))
            wave = None if legd is None else (legd["class"], bool(legd["at_extreme"]))
        if out["wob"] is None:
            out["wob"] = wob
            l0 = legd if mode == "wave" else wavefacts.leg(index, float(opened), float(now), strike, side, float(left))
            out["cls0"] = None if l0 is None else l0["class"]
        r = fallrule.decide(st, x, entry, fee(x), fee(entry), fee, wob, now, left, cushion, sunit, None, None, b["ts"], wave if mode == "wave" else "off")
        if r.sell:
            why = ""
            if mode == "wave" and legd is not None and r.kind in ("turn", "leg done"):
                why = f"{legd['class']}, swing {legd['swing_u']} u gave back {legd['gb_u']} u, travel {legd['travel']}, age {legd['age']}, {left}s left, +{r.net * 100:.1f}c"
            out.update(new=x - fee(x) - entry, kind=r.kind, at=now, px=x, why=why)
            break
    return out


res = {}
for mode in ("fall", "wave"):
    rows = []
    for b in bets:
        r = walk(b, mode)
        if r is not None:
            r["b"] = b
            rows.append(r)
    res[mode] = rows
n = len(res["wave"])
tot = lambda rows, k: sum(r[k] * r["b"]["n"] for r in rows)  # noqa: E731
print(f"window: {hm(min(r['b']['ts'] for r in res['wave']))} to {hm(max(r['b']['ts'] for r in res['wave']))} ET, {n} live PriskP bets (a bet with no tape, strike or result is left out)\n")
print(f"actual ${tot(res['wave'], 'actual'):8.2f}   held to settlement ${sum(((1.0 if r['b']['result'] == r['b']['side'] else 0.0) - r['b']['entry']) * r['b']['n'] for r in res['wave']):8.2f}   bid fall ${tot(res['fall'], 'new'):8.2f}   WAVE ${tot(res['wave'], 'new'):8.2f}")


def line(name: str, idxs: list[int]) -> str:
    if not idxs:
        return f"  {name:26s} n   0"
    w, f = [res["wave"][i] for i in idxs], [res["fall"][i] for i in idxs]
    d = [(a["new"] - a["actual"]) * a["b"]["n"] for a in w]
    return f"  {name:26s} n {len(idxs):3d}  actual ${tot(w, 'actual'):8.2f}  bid fall ${tot(f, 'new'):8.2f}  wave ${tot(w, 'new'):8.2f}  wave against actual: beat {sum(x > 0.005 for x in d):3d} lost {sum(x < -0.005 for x in d):3d}"


allx = list(range(n))
print("\nby how the wave rule ends the bet:")
for k in sorted({r["kind"] for r in res["wave"]}):
    print(line(k, [i for i in allx if res["wave"][i]["kind"] == k]))
ws = sorted(r["wob"] for r in res["wave"] if r["wob"])
c1, c2 = ws[len(ws) // 3], ws[2 * len(ws) // 3]
print(f"\nby the bid's wobble at the buy (thirds at {c1:.3f} and {c2:.3f}):")
for name, f in (("calm", lambda w: w < c1), ("middle", lambda w: c1 <= w < c2), ("jumpy", lambda w: w >= c2)):
    print(line(name, [i for i in allx if res["wave"][i]["wob"] and f(res["wave"][i]["wob"])]))
print("\nby the leg's class for the bet's side at the buy:")
for k in sorted({str(r["cls0"]) for r in res["wave"]}):
    print(line(k, [i for i in allx if str(res["wave"][i]["cls0"]) == k]))
print("\nby day:")
for d in sorted({hm(r["b"]["ts"])[:5] for r in res["wave"]}):
    print(line(d, [i for i in allx if hm(res["wave"][i]["b"]["ts"])[:5] == d]))


def show(i: int) -> str:
    w, f = res["wave"][i], res["fall"][i]
    b = w["b"]
    end = "won" if b["result"] == b["side"] else "lost"
    act = f"{b['exit_reason'].split(':')[0][:22]} @{b['exit_price']:.2f}" if b["exit_ts"] else f"settled {end}"
    one = lambda r: (f"{r['kind']} {hm(r['at'])[6:]} @{r['px']:.2f}" if r["at"] else f"settled {end}")  # noqa: E731
    return f"  {b['ticker'][2:-3]:20s} {b['side']:3s} {hm(b['ts'])[6:]} paid {b['entry']:.2f} | actual {act:28s} ${w['actual'] * b['n']:+7.2f} | bid fall {one(f):26s} ${f['new'] * b['n']:+7.2f} | wave {one(w):26s} ${w['new'] * b['n']:+7.2f}  {w['why']}"


print("\nthe bets the user flagged:")
for i in allx:
    if res["wave"][i]["b"]["ticker"].startswith(FLAG):
        print(show(i))
d = sorted(allx, key=lambda i: (res["wave"][i]["new"] - res["wave"][i]["actual"]) * res["wave"][i]["b"]["n"])
print("\nthe 10 bets the wave rule does worst on against actual:")
for i in d[:10]:
    print(show(i))
print("\nthe 10 it does best on:")
for i in d[-10:][::-1]:
    print(show(i))
