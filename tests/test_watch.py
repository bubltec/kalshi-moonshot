from kalshi_moonshot.__main__ import watch_line


def test_a_quote_line_shows_both_sides_with_their_sizes():
    line = watch_line({"kind": "quote", "ts": 0.0, "ticker": "KXINXU-26OCT22H1300-T7669.9999", "bid": 0.73, "ask": 0.75,
                       "bid_size": 40000.0, "ask_size": 11.78, "last": 0.72})
    assert "bid 0.73 x  40,000" in line and "ask 0.75 x      12" in line and "last 0.72" in line


def test_a_trade_line_says_which_side_the_taker_bought():
    line = watch_line({"kind": "trade", "ts": 0.0, "ticker": "KXGOLD15M-1", "price": 0.57, "count": 11.18, "taker": "no"})
    assert "TRADE" in line and "11.2 contracts at 0.57" in line and "taker bought NO" in line
