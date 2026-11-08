import json
import math
import random
import time

from kalshi_moonshot import breadthcheck, config, ticks, variants
from kalshi_moonshot.store import Store


def _feed(now, drops):
    """1 s ticks for 20 minutes on every crypto index; the indexes named in `drops` fall by that many typical minutes over the last 60 s."""
    random.seed(4)
    ticks.history.clear()
    for ser in breadthcheck.SERIES:
        ix = variants.crypto_index(ser)
        p = 100.0
        for i in range(1200):
            t = now - 1200 + i
            if t > now - 65:  # the last minute: flat, except the indexes that fall (the noise of the rest of the walk is what sets the typical minute)
                p *= math.exp(-drops.get(ser, 0.0) * 1e-4 * math.sqrt(60) / 60) if t > now - 60 else 1.0
            else:
                p *= math.exp(random.gauss(0, 1e-4))
            ticks.history.note(ix, t, p)


def test_the_breadth_counts_the_indexes_moving_a_typical_minute_against_the_bet():
    now = time.time()
    _feed(now, {"KXBTC15M": 3.0, "KXETH15M": 3.0, "KXSOL15M": 0.3})  # two fall three typical minutes, one only 0.3
    try:
        b, seen = breadthcheck.breadth_against(1.0, now)  # a YES bet: down is against it
        assert seen == len(breadthcheck.SERIES) and b == 2
        assert breadthcheck.breadth_against(-1.0, now)[0] == 0  # a NO bet: the same falls are with it
    finally:
        ticks.history.clear()


def test_the_shadow_logs_once_per_bet_with_what_the_check_would_do(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    now = time.time()
    breadthcheck._seen.clear()
    _feed(now, {"KXBTC15M": 3.0, "KXETH15M": 3.0})
    try:
        breadthcheck.shadow_fire(s, "KXSOL15M-X", "crypto_multiscale", "yes", now, 0.69, 223.0, 2.0, -11.2)
        breadthcheck.shadow_fire(s, "KXSOL15M-X", "crypto_multiscale", "yes", now + 1, 0.60, 222.0, 1.0, -12.0)  # the same bet again: not logged
        rows = [json.loads(x) for x in (tmp_path / "breadth_shadow.jsonl").read_text().splitlines()]
        assert len(rows) == 1 and rows[0]["breadth"] == 2 and rows[0]["would"] == "sell now"
        _feed(now, {"KXBTC15M": 3.0})  # only one coin falling: isolated
        breadthcheck.shadow_fire(s, "KXSOL15M-Y", "crypto_multiscale", "yes", now, 0.69, 223.0, 2.0, -11.2)
        rows = [json.loads(x) for x in (tmp_path / "breadth_shadow.jsonl").read_text().splitlines()]
        assert rows[1]["breadth"] == 1 and rows[1]["would"] == f"wait {breadthcheck.P}s"
    finally:
        ticks.history.clear()
        breadthcheck._seen.clear()


def test_the_fired_bets_are_scored_against_holding_in_the_deferred_and_sell_now_groups(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    store = Store(s.db_path)
    now = time.time()
    for tk, side, result in (("KXSOL15M-A", "yes", "yes"), ("KXSOL15M-B", "yes", "no")):
        store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, result, fetched_ts) VALUES (?,?,?,?,?,?)", (tk, "KXSOL15M", now - 300, 100.0, result, now))
        store.db.execute(
            "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, exit_ts, exit_price, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now - 900, tk, "bet", "trade", 0.8, side, "crypto_multiscale", 4.0, 3.2, now - 600, 0.55, "shock exit: spot moved"),
        )
    store.db.commit()
    fires = [
        {"kind": "fire", "ts": now - 600, "ticker": "KXSOL15M-A", "lane": "crypto_multiscale", "side": "yes", "breadth": 1, "k": 2, "p": 3},  # isolated: deferred; it won held, so the sale lost to holding
        {"kind": "fire", "ts": now - 600, "ticker": "KXSOL15M-B", "lane": "crypto_multiscale", "side": "yes", "breadth": 5, "k": 2, "p": 3},  # broad: sold at once; it lost held, so the sale beat holding
    ]
    (tmp_path / "breadth_shadow.jsonl").write_text("\n".join(json.dumps(f) for f in fires) + "\n")
    breadthcheck._scored_at = 0.0
    new = breadthcheck.run_score(s, store, now=now)
    got = {r["ticker"]: r for r in new}
    assert got["KXSOL15M-A"]["defer"] is True and got["KXSOL15M-A"]["change"] < -1.0  # sold at 0.55 against a 4.0 payout
    assert got["KXSOL15M-B"]["defer"] is False and got["KXSOL15M-B"]["change"] > 1.0
    breadthcheck._scored_at = 0.0
    assert breadthcheck.run_score(s, store, now=now + 4000) == []  # already scored
