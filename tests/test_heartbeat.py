from kalshi_moonshot import heartbeat


class Fake:
    def __init__(self):
        self.calls = []

    def put_metric_data(self, **kw):
        self.calls.append(kw)


def test_sends_one_point_while_a_job_or_the_stream_is_recent():
    c = Fake()
    assert heartbeat.beat_once(c, last_progress=1000.0, stream_beat=0.0, now=1100.0)
    assert heartbeat.beat_once(c, last_progress=0.0, stream_beat=1000.0, now=1100.0)
    assert c.calls[0]["Namespace"] == "KalshiMoonshot/Bot" and c.calls[0]["MetricData"][0]["MetricName"] == "Heartbeat"


def test_sends_nothing_when_both_are_stale():
    c = Fake()
    assert not heartbeat.beat_once(c, last_progress=1000.0, stream_beat=1000.0, now=1000.0 + heartbeat.STALE_SECONDS + 1)
    assert c.calls == []
