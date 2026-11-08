"""Timestamped news intake and read-only access to existing Apify datasets.

News text is untrusted data. This module never executes its contents, treats
headlines as order instructions, or starts an Apify actor. Publication time
and the time this bot first observes an article are separate facts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

DEFAULT_FIELDS = {
    "title": "title", "url": "url", "body": "text",
    "source": "source", "published": "publishedAt",
}
MAX_DATASET_BYTES = 5 * 1024 * 1024
MAX_BODY_CHARS = 100_000
MAX_TIMESTAMP = 2**63 - 1  # SQLite INTEGER storage


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Prevent a remote redirect from forwarding dataset credentials or
        # moving a read to an unrelated host/protocol.
        return None


def _observation_time(value: int) -> int:
    if type(value) is not int or not 0 < value <= MAX_TIMESTAMP:
        raise ValueError("observed_at_ms must be a positive integer millisecond timestamp")
    return value


def _published_time(value) -> int:
    if type(value) is int:
        if not 1_000_000_000_000 <= value <= MAX_TIMESTAMP:
            raise ValueError("Publication timestamps must use integer milliseconds, not seconds")
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timezone missing")
            milliseconds = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
            if not 0 < milliseconds <= MAX_TIMESTAMP:
                raise ValueError("timestamp outside supported range")
            return milliseconds
        except (ValueError, OverflowError, OSError):
            pass
    raise ValueError("Publication time must be integer milliseconds or timezone-aware ISO 8601")


def _article_url(value) -> tuple[str, str]:
    if not isinstance(value, str) or not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("Article URL must be HTTP(S), without whitespace or credentials")
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("invalid URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("credentials forbidden")
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        if not hostname or any(ch in hostname for ch in "\\/%?#"):
            raise ValueError("invalid host")
        port = parsed.port
        if port is not None and not 0 < port <= 65535:
            raise ValueError("invalid port")
        host = f"[{hostname}]" if ":" in hostname else hostname
        netloc = host if port is None else f"{host}:{port}"
        url = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        raise ValueError("Article URL must be HTTP(S), without whitespace or credentials") from None
    return url, hostname


def _asset_symbols(assets: list[str]) -> list[str]:
    if not isinstance(assets, list) or not assets:
        raise ValueError("At least one explicit asset ticker is required")
    normalized = []
    for value in assets:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", value.strip().upper()):
            raise ValueError("Asset tickers must contain 1-20 letters or digits")
        normalized.append(value.strip().upper())
    return sorted(set(normalized))


def _fields(field_map: dict[str, str] | None) -> dict[str, str]:
    fields = dict(DEFAULT_FIELDS)
    if field_map is not None:
        if not isinstance(field_map, dict) or any(
            key not in fields or not isinstance(value, str) or not value.strip()
            for key, value in field_map.items()
        ):
            raise ValueError("Field map must map title/url/body/source/published to nonempty flat field names")
        fields.update(field_map)
    return fields


def normalize_article(
    item: dict,
    *,
    observed_at_ms: int,
    assets: list[str],
    field_map: dict[str, str] | None = None,
) -> dict:
    """Normalize a record without trusting a provider's claimed observation time.

    Explicit asset tags describe the configured feed; they are not inferred
    from headline keywords. Missing publication timestamps are rejected.
    """
    observed = _observation_time(observed_at_ms)
    symbols = _asset_symbols(assets)
    fields = _fields(field_map)
    if not isinstance(item, dict):
        raise ValueError("Article record must be a JSON object")
    title = item.get(fields["title"])
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 1000:
        raise ValueError("Article title must contain 1-1000 characters")
    url, hostname = _article_url(item.get(fields["url"]))
    published = _published_time(item.get(fields["published"]))
    if published > observed:
        raise ValueError("Publication time is later than this bot's observation time")
    body = item.get(fields["body"], "")
    source = item.get(fields["source"], hostname)
    if body is None:
        body = ""
    if source is None or source == "":
        source = hostname
    if not isinstance(body, str) or len(body) > MAX_BODY_CHARS:
        raise ValueError("Article body must be text of at most 100000 characters")
    if not isinstance(source, str) or not source.strip() or len(source.strip()) > 500:
        raise ValueError("Article source must contain 1-500 characters")
    return {
        "id": hashlib.sha256(url.encode()).hexdigest(),
        "url": url, "title": title.strip(), "body": body,
        "source": source.strip(), "published_at_ms": published,
        "observed_at_ms": observed, "assets": symbols,
    }


def ingest_items(
    memory,
    items: list,
    *,
    observed_at_ms: int,
    assets: list[str],
    field_map: dict[str, str] | None = None,
) -> dict:
    """Import records atomically; bad rows are counted with content-free errors."""
    _observation_time(observed_at_ms)
    _asset_symbols(assets)
    _fields(field_map)
    if not isinstance(items, list):
        raise ValueError("News input must be a JSON array")
    report = {"inserted": 0, "duplicates": 0, "rejected": 0, "errors": []}
    with memory.transaction():
        for index, item in enumerate(items):
            try:
                article = normalize_article(item, observed_at_ms=observed_at_ms,
                                            assets=assets, field_map=field_map)
            except ValueError as exc:
                report["rejected"] += 1
                report["errors"].append({"index": index, "error": str(exc)})
                continue
            key = "inserted" if memory.insert_news(article) else "duplicates"
            report[key] += 1
    return report


def fetch_apify_dataset(
    dataset_id: str,
    *,
    token: str | None = None,
    limit: int = 100,
    offset: int = 0,
    descending: bool = True,
    timeout: float = 20,
) -> list[dict]:
    """Read one bounded page of an existing dataset; never launch an actor.

    Public datasets need no token. Private access uses an Authorization
    header, never a query-string credential. Callers control pagination.
    """
    if not isinstance(dataset_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", dataset_id):
        raise ValueError("Apify dataset ID must contain 1-100 letters, digits, underscores or hyphens")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Apify dataset page limit must be an integer in [1, 1000]")
    if type(offset) is not int or offset < 0:
        raise ValueError("Apify dataset offset must be a nonnegative integer")
    if type(descending) is not bool:
        raise ValueError("Apify descending must be a boolean")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Apify timeout must be finite and positive")
    if token is not None and (not isinstance(token, str) or not token or any(not 33 <= ord(ch) <= 126 for ch in token)):
        raise ValueError("Apify token must be a nonempty printable ASCII value without whitespace")
    query = urllib.parse.urlencode({"format": "json", "clean": "true", "limit": limit,
                                   "offset": offset, "desc": str(descending).lower()})
    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?{query}"
    headers = {"Accept": "application/json", "User-Agent": "tradingbot/news (read-only dataset client)"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_DATASET_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Apify dataset read failed (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        # Network exceptions can contain complete request URLs or proxy details.
        raise RuntimeError("Apify dataset read failed (network or TLS error)") from None
    if len(raw) > MAX_DATASET_BYTES:
        raise RuntimeError("Apify dataset page exceeds the 5 MiB response limit")
    try:
        items = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("Apify dataset response is not valid JSON") from None
    if not isinstance(items, list) or len(items) > limit or any(not isinstance(item, dict) for item in items):
        raise RuntimeError("Apify dataset response must be a bounded array of article objects")
    return items


def _xml_text(element, names: tuple[str, ...]) -> str:
    for name in names:
        for child in element:
            if child.tag.rsplit("}", 1)[-1] == name:
                return "".join(child.itertext()).strip()
    return ""


def _feed_publication(value: str) -> str | None:
    """Accept explicit ISO/RFC 2822 dates; never guess a missing timezone."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, IndexError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def fetch_rss(url: str, *, limit: int = 100, timeout: float = 20) -> list[dict]:
    """Read a bounded public RSS/Atom feed into the same news import schema.

    Entries without an explicit publication date and timezone are omitted,
    rather than claiming that undated content was published at collection.
    No authorization header or actor invocation is involved.
    """
    feed_url, hostname = _article_url(url)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("RSS item limit must be an integer in [1, 1000]")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("RSS timeout must be finite and positive")
    request = urllib.request.Request(feed_url, headers={
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        "User-Agent": "tradingbot/news (public feed reader)",
    }, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_DATASET_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"RSS feed read failed (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise RuntimeError("RSS feed read failed (network or TLS error)") from None
    if len(raw) > MAX_DATASET_BYTES:
        raise RuntimeError("RSS feed exceeds the 5 MiB response limit")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError):
        raise RuntimeError("RSS feed response is not valid XML") from None
    if root.tag.rsplit("}", 1)[-1] not in ("rss", "feed", "RDF"):
        raise RuntimeError("RSS feed response is not an RSS or Atom document")

    items = []
    for element in root.iter():
        kind = element.tag.rsplit("}", 1)[-1]
        if kind not in ("item", "entry"):
            continue
        title = _xml_text(element, ("title",))
        publication = _feed_publication(_xml_text(element, ("published", "pubDate", "date")))
        if not title or publication is None:
            continue
        link = _xml_text(element, ("link",)) if kind == "item" else ""
        if kind == "entry":
            for child in element:
                if child.tag.rsplit("}", 1)[-1] == "link" and child.get("rel", "alternate") == "alternate":
                    link = child.get("href", "")
                    if link:
                        break
        try:
            article_url, _ = _article_url(urllib.parse.urljoin(feed_url, link)) if link else (None, None)
        except ValueError:
            continue
        if article_url is None:
            continue
        items.append({
            "title": title, "url": article_url,
            "text": _xml_text(element, ("encoded", "content", "description", "summary")),
            "source": hostname, "publishedAt": publication,
        })
        if len(items) >= limit:
            break
    return items
