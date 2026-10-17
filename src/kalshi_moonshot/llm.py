"""Local model via Ollama. Used for one narrow job: estimate the probability of a single event.

The model is NOT shown the market price. Showing it would anchor the answer to the crowd and
make the estimate useless as an independent check.
"""

from __future__ import annotations

import json
import re

import httpx

PROMPT = """You estimate probabilities of future events. Be calibrated and skeptical: most
specific, unlikely-sounding events do not happen, and your first instinct is usually too high.
Use base rates. If the question depends on information you cannot know (live scores, today's
news), say so by staying close to a sensible base rate rather than guessing.

Event: {title}
Resolution rules: {rules}
Closes: {close_time}

Reply with ONLY JSON: {{"probability": <number between 0.001 and 0.999>, "rationale": "<one sentence>"}}"""


def parse_estimate(text: str) -> tuple[float, str]:
    """Pull probability and rationale out of model text, tolerating <think> blocks and fences."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError(f"no JSON in model reply: {text[:120]!r}")
    data = json.loads(m.group(0))
    p = float(data["probability"])
    if p > 1.0:  # model answered in percent
        p /= 100.0
    p = min(max(p, 0.001), 0.999)
    return p, str(data.get("rationale", ""))[:300]


class Ollama:
    def __init__(self, url: str, model: str):
        self.url = url
        self.model = model
        self._http = httpx.Client(timeout=180.0)

    def estimate(self, title: str, rules: str, close_time: str) -> tuple[float, str]:
        body = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {"temperature": 0.2},
            "messages": [
                {
                    "role": "user",
                    "content": PROMPT.format(
                        title=title, rules=rules[:1500], close_time=close_time
                    ),
                }
            ],
        }
        resp = self._http.post(f"{self.url}/api/chat", json=body)
        resp.raise_for_status()
        return parse_estimate(resp.json()["message"]["content"])

    def ping(self) -> bool:
        try:
            return self._http.get(f"{self.url}/api/tags", timeout=5.0).status_code == 200
        except httpx.HTTPError:
            return False
