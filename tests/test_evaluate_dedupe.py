from kalshi_moonshot.jobs import Jobs
from kalshi_moonshot.store import Store


def test_two_lanes_can_each_queue_the_same_market(tmp_path):
    jobs = Jobs(Store(tmp_path / "m.db"))
    first = jobs.enqueue("evaluate", dedupe_key="evaluate:fx_ticket:KXEURUSD15M-1")
    other = jobs.enqueue("evaluate", dedupe_key="evaluate:fx_trail30:KXEURUSD15M-1")
    again = jobs.enqueue("evaluate", dedupe_key="evaluate:fx_ticket:KXEURUSD15M-1")
    assert first is not None and other is not None  # a different lane is not dropped
    assert again is None  # the same lane asking twice still is
