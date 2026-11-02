from kalshi_moonshot.watcher import BURST_BID_SLACK, burst_bid_collapsed

WHY = "burst: $52 bought against the no holder in 3 s, 23.8x this market's normal $2 (bar 10x), the favorite's bid 0.91; the yes side costs 0.09"


def test_flickered_bid_holds_the_sale():
    assert burst_bid_collapsed(WHY, 0.19)
    assert burst_bid_collapsed(WHY, 0.91 - BURST_BID_SLACK - 0.01)


def test_bid_near_the_trigger_sells():
    assert not burst_bid_collapsed(WHY, 0.91)
    assert not burst_bid_collapsed(WHY, 0.80)


def test_other_exits_and_odd_reasons_are_untouched():
    assert not burst_bid_collapsed("strike-through: index stayed 0.03% through the strike", 0.10)
    assert not burst_bid_collapsed("burst: no price in this reason", 0.10)
