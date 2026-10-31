from kalshi_moonshot.kalshi import snap_price


def test_a_limit_is_put_on_the_tick_grid_in_both_ladder_zones():
    assert snap_price(0.143) == 0.14  # over 10c the step is a cent: the live 0.093 + 0.05 that Kalshi refused as invalid_price
    assert snap_price(0.148) == 0.14 and snap_price(0.148, up=True) == 0.15
    assert snap_price(0.118) == 0.11
    assert snap_price(0.093) == 0.093 and snap_price(0.0934) == 0.093  # under 10c the step is a tenth of a cent
    assert snap_price(0.0934, up=True) == 0.094
    assert snap_price(0.913) == 0.913 and snap_price(0.9234) == 0.923  # and over 90c
    assert snap_price(0.50) == 0.5 and snap_price(0.19) == 0.19  # a price already on the grid does not move
    assert snap_price(0.10) == 0.1 and snap_price(0.90) == 0.9


def test_a_price_on_the_grid_is_never_moved_by_float_noise():
    for cents in range(1, 100):
        p = cents / 100.0
        assert snap_price(p) == snap_price(p, up=True) == round(p, 4), p
    assert snap_price(1.0 - 0.15) == 0.85 and snap_price(1.0 - 0.18, up=True) == 0.82
