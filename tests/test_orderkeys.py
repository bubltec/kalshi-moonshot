from kalshi_moonshot import orderkeys


def setup_function(_):
    orderkeys.reset()


def test_the_next_attempt_has_one_id_until_the_order_is_sent():
    orderkeys.seed("demo", {})
    a = orderkeys.key("demo", "lane", "KX-1", "yes")
    assert a == orderkeys.key("demo", "lane", "KX-1", "yes")
    orderkeys.begin("demo", "KX-1", "yes")
    assert orderkeys.key("demo", "lane", "KX-1", "yes") != a  # a deliberate re-buy gets a new id


def test_a_restart_that_finds_no_order_row_repeats_the_id_so_kalshi_refuses_the_duplicate():
    orderkeys.seed("demo", {})
    sent = orderkeys.key("demo", "lane", "KX-1", "no")
    orderkeys.begin("demo", "KX-1", "no")  # set before the order is sent, then the process dies before the row is written
    orderkeys.reset()
    orderkeys.seed("demo", {})  # the orders table has no row: the count is as before
    assert orderkeys.key("demo", "lane", "KX-1", "no") == sent


def test_a_written_order_row_moves_the_id_on_after_a_restart():
    orderkeys.seed("demo", {})
    sent = orderkeys.key("demo", "lane", "KX-1", "no")
    orderkeys.reset()
    orderkeys.seed("demo", {("KX-1", "no"): 1})
    assert orderkeys.key("demo", "lane", "KX-1", "no") != sent


def test_nothing_is_ready_before_the_seed():
    assert not orderkeys.ready()
    orderkeys.seed("demo", {})
    assert orderkeys.ready()


def test_ids_differ_by_lane_market_and_side():
    orderkeys.seed("demo", {})
    ids = {orderkeys.key("demo", l, t, sd) for l in ("a", "b") for t in ("X", "Y") for sd in ("yes", "no")}
    assert len(ids) == 8
