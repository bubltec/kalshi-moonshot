import time

from kalshi_moonshot import __main__ as cli
from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient


class Posted(KalshiClient):
    def __init__(self):
        super().__init__("https://example.invalid/trade-api/v2")
        self.sent: list[dict] = []

    def _request(self, method, path, *, params=None, json=None, auth=False):
        self.sent.append({"method": method, "path": path, "json": json})
        return {"order_id": "ORD1", "average_fill_price": "0.5200", "fill_count": "0.00"}


def test_a_resting_yes_buy_is_a_post_only_bid_that_cancels_itself():
    c = Posted()
    before = time.time()
    c.buy_resting("KXT-1", "yes", 2, 0.52, 180)
    body = c.sent[0]["json"]
    assert c.sent[0]["path"] == "/portfolio/events/orders"
    assert (body["side"], body["price"], body["count"]) == ("bid", "0.5200", "2.00")
    assert body["time_in_force"] == "good_till_canceled" and body["post_only"] is True
    assert before + 179 <= body["expiration_time"] <= time.time() + 181
    assert "immediate_or_cancel" not in str(body)


def test_a_resting_no_buy_is_an_ask_on_the_yes_book_at_one_minus_the_price():
    c = Posted()
    c.buy_resting("KXT-1", "no", 1, 0.47, 60)
    body = c.sent[0]["json"]
    assert (body["side"], body["price"]) == ("ask", "0.5300")  # buying NO at 0.47 is offering YES at 0.53


class Market:
    authed = True

    def __init__(self, status="active", bid="0.5200", ask="0.5300"):
        self.bought: list = []
        self.m = {"status": status, "yes_bid_dollars": bid, "yes_ask_dollars": ask, "title": "Aurora Gaming wins"}

    def market(self, ticker):
        return self.m

    def buy_resting(self, *a, **k):
        self.bought.append((a, k))
        return {"order_id": "ORD1"}

    def portfolio_rows(self, *a, **k):
        return []


def run(monkeypatch, capsys, tmp_path, argv, market=None):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOW_REAL_MONEY", "true")
    client = market or Market()
    monkeypatch.setattr(cli, "_client", lambda s: client)
    code = cli.cmd_probe_maker(config.load(), cli.build_parser().parse_args(["probe-maker", *argv]))
    return code, capsys.readouterr().out, client


def test_the_probe_is_a_dry_run_one_cent_under_the_ask_unless_told_to_send(monkeypatch, capsys, tmp_path):
    code, out, client = run(monkeypatch, capsys, tmp_path, ["KXT-1"])
    assert code == 0 and "buy 1 yes at 0.52" in out and "dry run: nothing sent" in out and client.bought == []


def test_it_refuses_a_price_that_would_cross_the_book_or_never_fill(monkeypatch, capsys, tmp_path):
    for argv, why in ((["KXT-1", "--price", "0.53"], "not under the ask"), (["KXT-1", "--price", "0.30"], "unlikely to fill"), (["KXT-1", "--count", "9"], "count must be")):
        code, out, client = run(monkeypatch, capsys, tmp_path, [*argv, "--yes"])
        assert code == 1 and why in out and client.bought == []
    code, out, client = run(monkeypatch, capsys, tmp_path, ["KXT-1", "--yes"], Market(status="closed"))
    assert code == 1 and "not open" in out and client.bought == []


def fake_clock(monkeypatch):
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    monkeypatch.setattr(time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))


def test_it_sends_one_resting_order_only_with_yes(monkeypatch, capsys, tmp_path):
    fake_clock(monkeypatch)
    code, out, client = run(monkeypatch, capsys, tmp_path, ["KXT-1", "--yes", "--seconds", "10"])
    assert code == 0 and len(client.bought) == 1 and client.bought[0][0][:4] == ("KXT-1", "yes", 1.0, 0.52)
    assert "expired unfilled" in out


def test_it_will_not_send_with_real_money_off(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KALSHI_ENV", "prod")
    monkeypatch.setenv("ALLOW_REAL_MONEY", "false")
    client = Market()
    monkeypatch.setattr(cli, "_client", lambda s: client)
    s = config.load()
    code = cli.cmd_probe_maker(s, cli.build_parser().parse_args(["probe-maker", "KXT-1", "--yes"]))
    out = capsys.readouterr().out
    assert s.env == "prod" and not s.allow_real_money
    assert code == 1 and "real money is off" in out and client.bought == []
