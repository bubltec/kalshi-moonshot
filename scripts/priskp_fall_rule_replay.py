"""Does the one exit rule (src/kalshi_moonshot/fallrule.py) beat the exits the live PriskP bets actually got? (2026-11-26; user: "We keep adding patches instead of fixing the core issue", "we need to consolidate one rule that handles them all", "One rule. For sure".)
QUESTION (one, on the same bets): every live PriskP bet since the per-tick entry change (2026-11-25 15:57 ET), walked second by second from its buy on its own taped quotes with the one rule as the whole exit (early profit, no loss, salvage, the time backstop, the top) in place of top_take > early_take > late_dip > no_recovery > wave_take > value_sale. What does each bet make against what it actually made?
HOW A BET IS SCORED: the rule is walked from the buy to the close. Its first sale is the result; with no sale the bet settles on the market's own result. A sale is at that second's bid less the sale fee 0.07 p (1 - p); the buys rested (no buy fee), the same for both sides of the comparison, though the rule itself judges profit after both fees as the live steps do.
HONEST LIMITS: the bid is the tape's last quote of each second, no fill modelled (a collapsing bid may not fill at the printed price). The spot is the settlement index's 60 s average against the market's strike; the live step reads the Kalshi perp adjusted to the strike where it has one, takes the better of the 60 s and 5 s averages for the cross test and asks the settlement-window veto before a loss sale, none of which is walked here. The wobble is the mid's typical minute since the open once there are 90 s of it. The rule's numbers are the ones early_take, late_dip and no_recovery ran with, except the fade (a curve for "about 10 mins"), whose power was 4 on the first run and 8 after seeing that 4 missed BTC 17:00, and the nearest-tick comparison, added after seeing BTC 17:00 sit 0.0003 from its bar. One and a half days of bets: a lead, scored forward from here.
SHAPES: thirds of the bet's own wobble at the buy (the bid's typical 10 s move) and the spot's cushion in sigma at the buy; no asset.
Run: PYTHONPATH=src .venv/bin/python scripts/priskp_fall_rule_replay.py TAPE.json.gz   (the tape from scripts/priskp_exit_tape_pull.py)"""
import datetime
import gzip
import json
import math
import sys

from kalshi_moonshot import fallrule

ET = datetime.timezone(datetime.timedelta(hours=-5))
TICK = 0.01
FLAG = ("KXNEAR15M-26NOV262100", "KXBTC15M-26NOV261700", "KXXRP15M-26NOV261700", "KXSOL15M-26NOV262030", "KXDOGE15M-26NOV262045", "KXDOGE15M-26NOV251945", "KXSOL15M-26NOV260830", "KXHYPE15M-26NOV261200", "KXDOGE15M-26NOV261145", "KXHYPE15M-26NOV252030", "KXZEC15M-26NOV261545", "KXSOL15M-26NOV251700", "KXDOGE15M-26NOV251730")


def fee(p: float) -> float:
    return 0.07 * p * (1.0 - p)


def hm(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M:%S")


def walk(b: dict) -> dict | None:
    close, side, entry = b["close"], b["side"], b["entry"]
    bid, ask, idx, strike = b["bid"], b["ask"], b["idx"], b["strike"]
    if len(bid) != 900 or len(idx) != 1520 or not strike or b["result"] not in ("yes", "no"):
        return None
    q0, i0 = close - 900, close - 1520  # bid[k] is second q0 + 1 + k; idx[k] is second i0 + 1 + k
    sb = [None if (x is None or y is None) else (x if side == "yes" else 1.0 - y) for x, y in zip(bid, ask, strict=True)]
    mid = [None if (x is None or y is None) else (x + y) / 2.0 for x, y in zip(bid, ask, strict=True)]
    sq, cnt = [0.0], [0]  # prefix sums of the mid's squared 1 s changes
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
    actual = (actual_sale - fee(actual_sale) if actual_sale is not None else won) - entry
    kept = False  # the one rule is the whole chain: wave_take and value_sale are not kept either
    stop_at = b["exit_ts"] if (b["exit_ts"] and kept) else close - 1.0
    st = fallrule.State()
    last_ahead, first = 0.0, None
    k_buy = int(math.ceil(b["ts"])) + 1 - (q0 + 1)
    out = {"actual": actual, "new": actual if kept else won - entry, "kind": "as actual" if kept else "settled", "at": None, "px": None, "wob": None, "z0": None, "why": ""}
    for k in range(max(k_buy, 1), 900):
        now = q0 + 1 + k
        if now >= stop_at:
            break
        x = sb[k]
        if x is None or x <= 0:
            continue
        left = close - now
        unit = math.sqrt(60.0 * (sq[k] - sq[0]) / cnt[k]) if (k >= 90 and cnt[k] >= 60 and sq[k] > 0) else None
        wob = max(TICK, unit * math.sqrt(10.0 / 60.0)) if unit else TICK
        j = now - (i0 + 1)  # index of this second in idx
        cushion = sunit = None
        if j >= 600 and iok[j + 1] - iok[j - 59] == 60 and icnt[j + 1] - icnt[j - 599] >= 60:
            avg = (isum[j + 1] - isum[j - 59]) / 60.0
            sunit = math.sqrt(60.0 * (isq[j + 1] - isq[j - 599]) / (icnt[j + 1] - icnt[j - 599]))
            cushion = (avg - strike) if side == "yes" else (strike - avg)
        if out["wob"] is None:
            out["wob"], out["z0"] = wob, fallrule.cushion_z(cushion, sunit, left)
            first = now
        net = x - fee(x) - (entry + fee(entry))
        if net > 0:
            last_ahead = now
        sale, kind, why = None, "", ""
        if True:
            r = fallrule.decide(st, x, entry, fee(x), fee(entry), fee, wob, now, left, cushion, sunit)
            if r.sell:
                sale, kind = x, r.kind
                why = "" if r.kind in ("top", "no recovery") else f"high {r.high:.2f} fell {r.fall:.3f} (bar {r.bar:.3f}, wobble {wob:.3f}) z {'n/a' if r.z is None else f'{r.z:+.2f}'} {left:.0f}s left"
        if sale is not None:
            out.update(new=sale - fee(sale) - entry, kind=kind, at=now, px=sale, why=why)
            break
    return out


bets = json.load(gzip.open(sys.argv[1]))
rows = []
for b in bets:
    r = walk(b)
    if r is None:
        print("skipped (no tape, strike or result):", b["ticker"], hm(b["ts"]))
        continue
    r["b"] = b
    r["d"] = (r["new"] - r["actual"]) * b["n"]
    rows.append(r)


def line(name: str, rs: list) -> str:
    if not rs:
        return f"{name:34s} n   0"
    a, n = sum(r["actual"] * r["b"]["n"] for r in rs), sum(r["new"] * r["b"]["n"] for r in rs)
    return f"{name:34s} n {len(rs):3d}  actual ${a:8.2f}  one rule ${n:8.2f}  change ${n - a:+8.2f}  beat {sum(r['d'] > 0.005 for r in rs):3d}  lost {sum(r['d'] < -0.005 for r in rs):3d}  same {sum(abs(r['d']) <= 0.005 for r in rs):3d}"


print(f"window: {hm(min(r['b']['ts'] for r in rows))} to {hm(max(r['b']['ts'] for r in rows))} ET, {len(rows)} live PriskP bets\n")
print(line("ALL", rows))
print("\nby how the bet actually ended:")
for k in sorted({(r["b"]["exit_reason"].split(":")[0] or "held to settlement") for r in rows}):
    print(" ", line(k, [r for r in rows if (r["b"]["exit_reason"].split(":")[0] or "held to settlement") == k]))
print("\nby how the one rule ends it:")
for k in sorted({r["kind"] for r in rows}):
    print(" ", line(k, [r for r in rows if r["kind"] == k]))
ws = sorted(r["wob"] for r in rows if r["wob"])
c1, c2 = ws[len(ws) // 3], ws[2 * len(ws) // 3]
print(f"\nby the bid's wobble at the buy (thirds at {c1:.3f} and {c2:.3f}):")
for name, f in (("calm", lambda w: w < c1), ("middle", lambda w: c1 <= w < c2), ("jumpy", lambda w: w >= c2)):
    print(" ", line(name, [r for r in rows if r["wob"] and f(r["wob"])]))
print("\nby the spot's cushion at the buy, in sigma of the time left:")
for name, f in (("z under 0 (wrong side)", lambda z: z < 0), ("z 0 to 0.5", lambda z: 0 <= z < 0.5), ("z 0.5 to 1", lambda z: 0.5 <= z < 1), ("z 1 and over", lambda z: z >= 1)):
    print(" ", line(name, [r for r in rows if r["z0"] is not None and f(r["z0"])]))
print(" ", line("unread", [r for r in rows if r["z0"] is None]))
print("\nby day:")
for d in sorted({hm(r["b"]["ts"])[:5] for r in rows}):
    print(" ", line(d, [r for r in rows if hm(r["b"]["ts"])[:5] == d]))


def show(r: dict) -> str:
    b = r["b"]
    act = f"{b['exit_reason'].split(':')[0]} {hm(b['exit_ts'])[6:]} @{b['exit_price']:.2f}" if b["exit_ts"] else f"settled {'won' if b['result'] == b['side'] else 'lost'}"
    new = f"{r['kind']} {hm(r['at'])[6:]} @{r['px']:.2f}" if r["at"] else r["kind"] + (f" ({'won' if b['result'] == b['side'] else 'lost'})" if r["kind"] == "settled" else "")
    return f"{b['ticker'][2:-3]:22s} {b['side']:3s} {hm(b['ts'])} paid {b['entry']:.2f} | actual: {act:38s} ${r['actual'] * b['n']:+7.2f} | one rule: {new:30s} ${r['new'] * b['n']:+7.2f} | {r['d']:+7.2f}  {r['why']}"


print("\nthe bets the user flagged:")
for r in rows:
    if r["b"]["ticker"].startswith(FLAG):
        print(" ", show(r))
print("\nthe 12 bets the one rule does worst on:")
for r in sorted(rows, key=lambda r: r["d"])[:12]:
    print(" ", show(r))
print("\nthe 12 it does best on:")
for r in sorted(rows, key=lambda r: -r["d"])[:12]:
    print(" ", show(r))
