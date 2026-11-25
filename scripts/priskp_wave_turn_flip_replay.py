"""The real wave_turn step, run every second on the 08:30 ET round of 2026-11-25, with flips (user: "exactly it should have re-evaluated on every tick and found where it buys on the updated logic"; "you are holding to the bought side instead of flipping it").
QUESTION (one): if the lane had asked waveturn.admits (the wave lane's own buy step, the study on the user's marked buys) for both sides on every second of each market from 60 s to 840 s, from the tick memory as it would have stood at that second, where would it have bought, where would it have flipped, and what did the sequence net per contract (fee on every leg, sales at the bid, buys at the ask, every market settled YES)? Shown with no price band (the wave lane's own, 0.01-0.99) and inside PriskP's 0.55-0.85.
INPUT: a directory with h_<index>.raw (per-second spot from 3700 s before the open to 900 s after), q_<coin>.raw (5 s average yes bid and ask) and page/data.json (targets), pulled one query at a time from the host.
HONEST LIMITS: one round of seven markets, all settled YES (a late YES wins by construction); the step's own settings are the study's (chosen on the user's marks, not retuned here); the quotes are 5 s averages carried forward; a flip is made the second the other side fires and the held side has not; the step never sells, so a held side that stops firing is held to settlement unless the other side fires. A lead only.
Run on this Mac: uv run python scripts/priskp_wave_turn_flip_replay.py <dir>"""
import bisect
import json
import os
import re
import sys
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from kalshi_moonshot import config, ticks, waveturn  # noqa: E402
from kalshi_moonshot.strategy import LongShot  # noqa: E402

D = sys.argv[1]
OPEN = 1795612500
CLOSE = OPEN + 900
IDX = {"BNB": "BNBUSD_RTI", "BTC": "BRTI", "ETH": "ETHUSD_RTI", "HYPE": "HYPEUSD_RTI", "SOL": "SOLUSD_RTI", "XRP": "XRPUSD_RTI", "DOGE": "DOGEUSD_RTI"}
SER = {"BNB": "KXBNB15M", "BTC": "KXBTC15M", "ETH": "KXETH15M", "HYPE": "KXHYPE15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M", "DOGE": "KXDOGE15M"}


def fee(p):
    return 0.07 * p * (1 - p)


def spot(path):
    line = [x.strip() for x in open(path).read().split("\n") if re.match(r"^-?\d+:[\d.]+,", x.strip())][0]
    d = {int(k): float(v) for k, v in (p.split(":") for p in line.split(","))}
    T, V, last = [], [], None
    for s in range(-3700, 901):
        if s in d:
            last = d[s]
        if last is None:
            continue
        T.append(float(OPEN + s))
        V.append(last)
    return T, V


def quotes(path):
    line = [x.strip() for x in open(path).read().split("\n") if re.match(r"^-?\d+:[\d.]+:[\d.]+,", x.strip())][0]
    return {int(a): (float(b), float(c)) for a, b, c in (p.split(":") for p in line.split(","))}


class History:
    """The tick memory as the entry thread would see it: a gridded 1 s path per index."""

    def __init__(self):
        self.t, self.v = {}, {}

    def window(self, index, start, end):
        t = self.t.get(index)
        if not t or t[0] > start:
            return None
        lo, hi = bisect.bisect_left(t, start), bisect.bisect_right(t, end)
        return list(t[lo:hi]), list(self.v[index][lo:hi])

    def last_at_or_before(self, index, ts):
        t = self.t.get(index)
        if not t:
            return None
        i = bisect.bisect_right(t, ts) - 1
        return (t[i], self.v[index][i]) if i >= 0 else None

    def first_at_or_after(self, index, start, within):
        t = self.t.get(index)
        if not t or t[0] > start:
            return None
        i = bisect.bisect_left(t, start)
        return self.v[index][i] if i < len(t) and t[i] - start <= within else None


meta = {m["asset"]: m for m in json.load(open(f"{D}/page/data.json"))["markets"]}
s_cfg = config.load()
hist = History()
ticks.history = hist
for coin, ix in IDX.items():
    hist.t[ix], hist.v[ix] = spot(f"{D}/h_{ix}.raw")


def run(coin, lo, hi):
    ix, K = IDX[coin], meta[coin]["target"]
    q = quotes(f"{D}/q_{coin}.raw")
    held, paid, net, legs = None, 0.0, 0.0, []
    for now in range(60, 841, 1):
        qq = None
        for k in range(0, 15):
            qq = q.get(5 * ((now - k) // 5))
            if qq:
                break
        if qq is None:
            continue
        yb, ya = qq
        px = {"yes": (ya, yb), "no": (1.0 - yb, 1.0 - ya)}  # (ask, bid) per side
        fires = []
        for side in ("yes", "no"):
            sh = LongShot(f"{SER[coin]}-26NOV250830-30", "t", "", yb, ya, (CLOSE - (OPEN + now)) / 3600.0, "2099-01-01T00:00:00Z", (CLOSE - (OPEN + now)) / 3600.0, lane="x", series=SER[coin], side=side, event_ticker=f"{SER[coin]}-26NOV250830")
            ok, why = waveturn.admits(s_cfg, sh, float(OPEN + now), K)
            if ok:
                fires.append(side)
        for side in fires:
            if side == held:
                continue
            ask = px[side][0]
            if not (lo <= ask <= hi):
                continue
            if held is not None:
                bid = px[held][1]
                net += bid - fee(bid) - paid
                legs.append(f"sold {held.upper()} {bid:.2f} at {now / 60:.1f}m")
            held, paid = side, ask + fee(ask)
            legs.append(f"bought {side.upper()} {ask:.2f} at {now / 60:.1f}m")
            break
    if held is not None:
        net += (1.0 if held == "yes" else 0.0) - paid
        legs.append(f"held {held.upper()} to settlement")
    return net, legs, held is not None


for label, lo, hi in (("no price band (the wave lane's own)", 0.01, 0.99), ("inside PriskP's band 0.55-0.85", 0.55, 0.85)):
    print(f"\n== wave_turn on every second, with flips, {label}")
    total, n = 0.0, 0
    for coin in ("HYPE", "BNB", "BTC", "ETH", "SOL", "XRP", "DOGE"):
        net, legs, bought = run(coin, lo, hi)
        total += net
        n += bought
        print(f"{coin:5s} net {net:+.3f} per contract | " + (", ".join(legs) if legs else "no buy"))
    print(f"sum over {n} markets that bought: {total:+.3f} per contract")
