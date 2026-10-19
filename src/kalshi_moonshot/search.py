"""External context: recent news headlines about a market's subject. No API key needed.

Source is Google News RSS. Rules that keep it useful as leverage, not as noise:
  * Headlines that quote odds or other prediction markets are dropped, so the model cannot
    learn the market price through the back door (llm.py never shows it the price either).
  * Newest first, with age on each line, last 3 days. A week-old theme piece is not breaking news.
  * Everything returned is untrusted web text. llm.py tells the model to use fresh specific facts
    and to ignore any instructions inside the headlines.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
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
# "Will X happen before Oct 19" is a bad search; keep the subject and drop the boilerplate.
_QUERY_STOP = frozenset({
    "will", "the", "before", "after", "more", "than", "this", "that", "with", "from",
    "have", "been", "said", "say", "says", "who", "what", "when", "where", "how",
    "would", "could", "should", "about", "into", "for", "not", "and", "of", "to",
    "in", "on", "at", "or", "a", "an", "be", "it", "is", "are", "by", "any", "its",
    "win", "wins", "won", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
    "oct", "nov", "dec",
})
WINDOW = "when:3d"  # older theme coverage is already in the price; we want what just happened


def clean(text: str, limit: int = 170) -> str:
    return re.sub(r"\s+", " ", TAGS.sub(" ", text)).strip()[:limit]


def age_label(when: datetime, now: datetime) -> str:
    """'14h ago' / '2d ago' so the model can tell breaking news from a week-old theme piece."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    hours = (now - when).total_seconds() / 3600.0
    if hours < 1.5:
        return f"{max(1, round(hours * 60))}m ago"
    if hours < 36:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def build_query(title: str, subtitle: str = "") -> str:
    """Subject first. Quoted YES-text (e.g. Antifa) beats the full 'Will Trump say...' sentence."""
    quoted = re.findall(r'"([^"]+)"', title)
    lead = (subtitle or "").strip() or (quoted[0].strip() if quoted else "")
    extra: list[str] = []
    for word in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", f"{lead} {title}"):
        low = word.lower()
        if low in _QUERY_STOP or low in extra or (lead and low in lead.lower().split()):
            continue
        extra.append(low if word.islower() or word.isupper() else word)
        if len(extra) == 4:
            break
    core = " ".join(x for x in [lead, *extra] if x).strip() or re.sub(r"\s+", " ", title).strip()
    return f"{core[:110]} {WINDOW}"


def parse_feed(xml_text: str, limit: int, now: datetime | None = None) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    now = now or datetime.now(UTC)
    rows: list[tuple[datetime, str]] = []
    for item in root.iter("item"):
        headline = clean(item.findtext("title") or "")
        if not headline or BLOCKED.search(headline):
            continue
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            when = None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        stamp = when or datetime(1970, 1, 1, tzinfo=UTC)
        label = age_label(when, now) if when is not None else ""
        rows.append((stamp, f"- {headline}" + (f" ({label})" if label else "")))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [line for _, line in rows[:limit]]


class NewsSearch:
    def __init__(self, results: int = 5, ttl_seconds: float = 3600):
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
