from kalshi_moonshot.kalshi import KalshiClient


class Stub(KalshiClient):
    def __init__(self, answers):
        super().__init__("https://example.invalid/trade-api/v2")
        self.answers, self.calls = answers, []

    def _request(self, method, path, *, params=None, json=None, auth=False):
        self.calls.append(dict(params or {}))
        return self.answers["open" if (params or {}).get("status") == "open" else "all"]


def test_the_filtered_answer_is_used_when_it_has_markets():
    c = Stub({"open": {"markets": [{"ticker": "A", "status": "active"}]}, "all": {"markets": []}})
    assert [m["ticker"] for m in c.series_markets("S")] == ["A"] and len(c.calls) == 1


def test_an_empty_filtered_answer_falls_back_to_the_unfiltered_list_and_keeps_open_markets():
    everything = [{"ticker": "OLD", "status": "finalized"}, {"ticker": "NOW", "status": "active"}, {"ticker": "NEXT", "status": "initialized"}]
    c = Stub({"open": {"markets": []}, "all": {"markets": everything}})
    assert [m["ticker"] for m in c.series_markets("S")] == ["NOW"]
    assert "status" not in c.calls[-1]


def test_a_series_with_nothing_open_stays_empty():
    c = Stub({"open": {"markets": []}, "all": {"markets": [{"ticker": "OLD", "status": "finalized"}]}})
    assert c.series_markets("S") == []
