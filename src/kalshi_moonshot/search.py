"""External context: recent news headlines about a market's subject. No API key needed.

Source is Google News RSS. Two rules keep it from hurting the estimate:
  * Headlines that quote odds or other prediction markets are dropped, so the model cannot
    learn the market price through the back door (llm.py never shows it the price either).
  * Everything returned is untrusted web text. llm.py tells the model to use it as evidence
    only and to ignore any instructions inside it.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import httpx

log = logging.getLogger("moonshot")

RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
BLOCKED = re.compile(
    r"kalshi|polymarket|predictit|prediction market|betting|odds|sportsbook|draftkings|fanduel|"
    r"wager|moneyline",
    re.IGNORECASE,
)
TAGS = re.compile(r"<[^>]+>")


def clean(text: str, limit: int = 170) -> str:
    return re.sub(r"\s+", " ", TAGS.sub(" ", text)).strip()[:limit]


def build_query(title: str, subtitle: str = "") -> str:
    """Subject first. In a multi-outcome market the subtitle is the thing being priced."""
    words = f"{subtitle} {title}".strip()
    words = re.sub(r"[^\w\s.'-]", " ", words)
    words = re.sub(r"\s+", " ", words).strip()
    return f"{words[:110]} when:14d"


def parse_feed(xml_text: str, limit: int) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    lines: list[str] = []
    for item in root.iter("item"):
        headline = clean(item.findtext("title") or "")
        if not headline or BLOCKED.search(headline):
            continue
        when = ""
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "").strftime("%b %d")
        except (TypeError, ValueError):
            pass
        lines.append(f"- {headline}" + (f" ({when})" if when else ""))
        if len(lines) >= limit:
            break
    return lines


class NewsSearch:
    def __init__(self, results: int = 5, ttl_seconds: float = 6 * 3600):
        self.results = results
        self.ttl = ttl_seconds
        self._http = httpx.Client(timeout=10.0, headers={"User-Agent": "Mozilla/5.0"})
        self._cache: dict[str, tuple[float, str]] = {}

    def context(self, title: str, subtitle: str = "") -> str:
        """Headlines as plain text lines, or '' when nothing useful is found. Never raises."""
        query = build_query(title, subtitle)
        hit = self._cache.get(query)
        now = time.time()
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        try:
            resp = self._http.get(RSS.format(q=quote_plus(query)))
            resp.raise_for_status()
            text = "\n".join(parse_feed(resp.text, self.results))
        except Exception as exc:  # search is a bonus; the estimate still runs without it
            log.warning("news search failed for %r: %s", query[:60], exc)
            return ""
        self._cache[query] = (now, text)
        return text
