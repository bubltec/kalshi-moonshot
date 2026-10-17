import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kalshi_moonshot import kalshi, llm, report


def test_signature_covers_timestamp_method_and_path_without_query():
    key = Ed25519PrivateKey.generate()
    h = kalshi.signed_headers(
        key, "kid", "get", "https://x/trade-api/v2/portfolio/orders?limit=5", ts_ms=1700000000000
    )
    assert h["KALSHI-ACCESS-KEY"] == "kid"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(sig, b"1700000000000GET/trade-api/v2/portfolio/orders")


def test_parse_estimate_handles_think_blocks_and_percent():
    p, why = llm.parse_estimate('<think>hmm</think>```json\n{"probability": 7, "rationale": "r"}\n```')
    assert abs(p - 0.07) < 1e-9 and why == "r"


def test_parse_estimate_clamps():
    assert llm.parse_estimate('{"probability": 0}')[0] == 0.001
    assert llm.parse_estimate('{"probability": 1.0}')[0] == 0.999


def row(p, ask, outcome, action):
    return {"model_p": p, "ask": ask, "outcome": outcome, "action": action}


def test_report_prefers_the_better_forecaster():
    rows = [row(0.02, 0.10, "no", "pass"), row(0.9, 0.5, "yes", "bet"), row(0.1, 0.5, "no", "pass")]
    s = report.summarize(rows)
    assert s["model_beats_market"]
    assert s["bets"] == 1 and s["bet_wins"] == 1
