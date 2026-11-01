import json
import re
import time

from kalshi_moonshot import exporter

NOW = 1_800_000_000.0
FORBIDDEN_KEY = re.compile(r"stake|usd|pnl|price|bid|ask|cost|roi|dollar|contract|fee|ticker|entry|exit_|settled_ts|^ts$|time", re.I)
ALLOWED_TOP = set(exporter.PUBLIC_KEYS)
ALLOWED_LANE = set(exporter.PUBLIC_LANE_KEYS)


def bet(i, lane="crypto_favorite", mode="trade", side="yes", outcome="yes", exit_price=None, reason=None, age=3 * 86400.0, ask=0.80, stake=25.0):
    return {"ts": NOW - age - i, "ticker": f"KXBTC15M-26OCT311230-{i}", "lane": lane, "mode": mode, "ask": ask, "side": side, "outcome": outcome,
            "exit_price": exit_price, "exit_reason": reason, "stake_usd": stake, "settled_ts": NOW - age + 900}


def rows(n_won=20, n_lost=15):
    out = [bet(i) for i in range(n_won)]
    out += [bet(100 + i, outcome="no") for i in range(n_lost)]
    out += [bet(200, exit_price=0.50, reason="stop-loss: bid 0.50 is 38% under the 0.80 paid", outcome="no"), bet(201, outcome="", age=1000)]
    return out


def walk(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def test_public_file_has_counts_only_and_nothing_money_like():
    pub = exporter.build_public(rows(), NOW)
    assert set(pub) == ALLOWED_TOP
    lane = pub["lanes"][0]
    assert set(lane) == ALLOWED_LANE
    for key in list(pub) + list(lane):
        assert not FORBIDDEN_KEY.search(key), key
    text = json.dumps(pub)
    assert "$" not in text and "KXBTC15M-" not in text  # a kind of market, never a specific ticker
    assert set(lane["markets"]) == {"KXBTC15M"}
    assert (lane["held_won"], lane["held_lost"], lane["cashed_out"]) == (20, 15, 1)


def test_the_last_day_is_left_out_and_a_thin_lane_shows_no_rate():
    pub = exporter.build_public(rows(), NOW)
    lane = pub["lanes"][0]
    assert lane["bets"] == 36  # the bet 1000 s old is inside the delay
    assert lane["win_rate"] == round(20 / 36, 3)
    thin = exporter.build_public(rows(5, 2), NOW)["lanes"][0]
    assert thin["settled"] < exporter.MIN_SETTLED and thin["win_rate"] is None


def test_the_admin_file_carries_the_money():
    adm = exporter.build_admin(rows(), NOW)
    lane = adm["lanes"][0]
    assert lane["staked_usd"] > 0 and "stop-loss" in lane["exits"]
    assert sum(lane["daily_pnl_usd"].values()) - lane["pnl_usd"] < 0.05


def test_a_wider_row_cannot_reach_the_public_file():
    r = rows()
    for x in r:
        x["secret_balance"] = 123456.0
    assert "secret_balance" not in json.dumps(exporter.build_public(r, NOW))
    assert time.time() > 0
