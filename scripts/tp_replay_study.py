"""Would the past bets have profited with the take-profit at 1.1x? A counterfactual exit replay on the tape.

Every paper bet the four favorites lanes placed since a cutoff is re-run from its own entry against the
recorded quotes: the same hard stop (first touch of 45% under the price paid sells at that bid; the live
stop's 10-second confirmation is not replayed), but the sale is a RESTING sell at 1.1x the price paid,
filled at its price the first second the side's bid reaches it, exactly as a post-only order fills.
Whatever neither level touches is held to the market's result. Each bet's counterfactual return is set
beside what the lane actually booked (its exit price, or settlement), in dollars on the recorded stake.

Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/tp_replay_study.py
"""
import time
from datetime import UTC, datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

TP_X = 1.1
STOP = 0.45
LANES = ("commodity", "commodity_hourly", "fx_favorite", "crypto_favorite")


def side_bid(bid, ask, side):
    return bid if side == "yes" else 1.0 - ask


def replay(row, path):
    """(pnl, how) for one bet under the 1.1x rule, on its recorded quote path."""
    entry, side = float(row["ask"]), row["side"] or "yes"
    stake, n = float(row["stake_usd"] or 0.0), float(row["contracts"] or 0.0)
    target = entry * TP_X if entry * TP_X < 1.0 else None
    stop = entry * (1.0 - STOP)
    for _ts, bid, ask in path:
        b = side_bid(bid, ask, side)
        if target is not None and b >= target - 1e-9:
            return n * target - stake, f"sold at target {target:.2f}"
        if b <= stop + 1e-9:
            return n * b - stake, f"stopped at {b:.2f}"
    if row["outcome"] is None:
        return None, "still open"
    return (n - stake, "held: won") if row["outcome"] == side else (-stake, "held: lost")


def actual(row):
    """(pnl, how) the lane really booked on the same bet."""
    stake, n = float(row["stake_usd"] or 0.0), float(row["contracts"] or 0.0)
    if row["exit_ts"] is not None:
        return n * float(row["exit_price"]) - stake, (row["exit_reason"] or "exited").split(":")[0]
    if row["outcome"] is None:
        return None, "still open"
    return (n - stake, "held: won") if row["outcome"] == (row["side"] or "yes") else (-stake, "held: lost")


def window(cutoff, label):
    rows = main.db.execute(
        f"""SELECT ts, ticker, side, ask, contracts, stake_usd, outcome, exit_price, exit_ts, exit_reason, lane, close_time
            FROM decisions WHERE action='bet' AND lane IN ({','.join('?' * len(LANES))}) AND ts>=? ORDER BY ts""",
        (*LANES, cutoff),
    ).fetchall()
    cache: dict[str, list] = {}
    tot = {"real": 0.0, "x11": 0.0, "stake": 0.0}
    kinds: dict[str, list[int]] = {}
    scored = skipped = 0
    for r in rows:
        if not float(r["stake_usd"] or 0.0):
            skipped += 1
            continue
        if r["ticker"] not in cache:
            cache[r["ticker"]] = tape.execute(
                "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts>=? ORDER BY ts",
                (r["ticker"], float(r["ts"])),
            ).fetchall()
        mine, path = [], cache[r["ticker"]]
        try:  # the counterfactual position lives until the market closes, not until the real bet happened to exit
            end = datetime.fromisoformat(str(r["close_time"])).timestamp()
        except (TypeError, ValueError):
            end = float(r["ts"]) + 7200.0
        mine = [q for q in path if q[0] <= end]
        cf, how_cf = replay(r, mine)
        real, _how_real = actual(r)
        if cf is None or real is None:
            continue
        scored += 1
        tot["real"] += real
        tot["x11"] += cf
        tot["stake"] += float(r["stake_usd"])
        k = kinds.setdefault(how_cf.split(":")[0], [0, 0.0])
        k[0] += 1
        k[1] += cf
    print(f"\n{label}: {scored} bets with a recorded stake ({skipped} stake-less rows skipped)")
    if not scored:
        return
    print(f"  staked ${tot['stake']:.2f} | as it really went: ${tot['real']:+.2f} ({tot['real'] / tot['stake']:+.1%})"
          f" | with the sale at 1.1x: ${tot['x11']:+.2f} ({tot['x11'] / tot['stake']:+.1%})")
    for how, (n, pnl) in sorted(kinds.items(), key=lambda kv: kv[1][1]):
        print(f"    under 1.1x: {how:<14} {n:>3} bets, ${pnl:+.2f}")


now = time.time()
window(now - 3600.0, "the past hour")
window(datetime(2026, 10, 26, 14, 2, tzinfo=UTC).timestamp(), "since the new book (2026-10-26 10:02 ET)")
