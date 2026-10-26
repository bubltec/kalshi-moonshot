"""The one-order flip test script, against a fake account that behaves like a signed single book."""
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location("flip_order_test", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "flip_order_test.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class Fake:
    def __init__(self, flips=True, refuses=False):
        self.pos, self.flips, self.refuses = 0.0, flips, refuses

    def market(self, ticker):
        return {"yes_bid_dollars": "0.50", "yes_ask_dollars": "0.52"}

    def positions(self):
        return {"market_positions": [{"ticker": "T", "position_fp": str(self.pos)}]} if self.pos else {"market_positions": []}

    def buy_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.pos += count
        return {"fill_count": count, "average_fill_price": 0.52}

    def _request(self, method, path, json=None, auth=False, params=None):
        if self.refuses:
            from kalshi_moonshot.kalshi import KalshiError

            raise KalshiError("400 bad request")
        n = float(json["count"])
        self.pos -= n if self.flips else min(n, max(self.pos, 0.0))
        return {"fill_count": n}

    def sell_no_ioc(self, ticker, count, no_price, client_order_id=None):
        self.pos += count
        return {"fill_count": count}

    def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.pos -= count
        return {"fill_count": count}


def test_a_dry_run_sends_nothing():
    f = Fake()
    out = mod.run(f, "T", False, say=lambda *_: None)
    assert out["dry"] and f.pos == 0.0


def test_a_flip_is_recognized_and_cleaned_up():
    f = Fake(flips=True)
    out = mod.run(f, "T", True, say=lambda *_: None)
    assert out["after_buy"] == 1.0 and out["after_swap"] == -1.0 and out["verdict"].startswith("FLIPPED") and out["final"] == 0.0


def test_a_sell_only_what_was_held_is_recognized():
    f = Fake(flips=False)
    out = mod.run(f, "T", True, say=lambda *_: None)
    assert out["after_swap"] == 0.0 and out["verdict"].startswith("SOLD ONLY") and out["final"] == 0.0


def test_a_refusal_is_reported_and_the_yes_is_closed():
    f = Fake(refuses=True)
    out = mod.run(f, "T", True, say=lambda *_: None)
    assert "swap_error" in out and out["final"] == 0.0


def test_an_existing_position_stops_the_test():
    f = Fake()
    f.pos = 3.0
    assert mod.run(f, "T", True, say=lambda *_: None)["ok"] is False
