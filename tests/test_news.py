import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tradingbot.memory import TradingMemory
from tradingbot.news import MAX_DATASET_BYTES, _NoRedirect, fetch_apify_dataset, fetch_rss, ingest_items, normalize_article


OBSERVED = 1_799_020_800_000  # 2027-01-04T00:00:00Z
PUBLISHED = OBSERVED - 60_000


def article(**overrides):
    item = {
        "title": "A market update", "url": "https://News.Example.com/story?id=42#section",
        "text": "Untrusted article body, not an order.", "publishedAt": PUBLISHED,
    }
    item.update(overrides)
    return item


class TestNormalizeNews(unittest.TestCase):
    def normalize(self, item, **kwargs):
        options = {"observed_at_ms": OBSERVED, "assets": ["btc", "ETH", "BTC"]}
        options.update(kwargs)
        return normalize_article(item, **options)

    def test_url_identity_publication_and_explicit_asset_normalization(self):
        result = self.normalize(article(observedAt=1, source=None))
        expected_url = "https://news.example.com/story?id=42"
        self.assertEqual(result["url"], expected_url)
        self.assertEqual(result["id"], hashlib.sha256(expected_url.encode()).hexdigest())
        self.assertEqual(result["assets"], ["BTC", "ETH"])
        self.assertEqual(result["source"], "news.example.com")
        self.assertEqual(result["observed_at_ms"], OBSERVED)
        self.assertEqual(result["published_at_ms"], PUBLISHED)
        self.assertEqual(result["body"], article()["text"])

    def test_timezone_aware_iso_timestamp_and_custom_actor_fields(self):
        item = {"headline": "  News title  ", "link": "https://example.com/a", "date": "2027-01-03T23:59:00+00:00"}
        result = self.normalize(item, field_map={"title": "headline", "url": "link", "published": "date"})
        self.assertEqual(result["title"], "News title")
        self.assertEqual(result["body"], "")
        self.assertEqual(result["published_at_ms"], PUBLISHED)
        self.assertEqual(self.normalize(article(publishedAt="2027-01-04T00:59:00+01:00"))["published_at_ms"], PUBLISHED)
        self.assertEqual(self.normalize(article(publishedAt="2027-01-03T23:59:00Z"))["published_at_ms"], PUBLISHED)

    def test_rejects_missing_ambiguous_nonfinite_future_and_naive_publication(self):
        for value in (None, "", PUBLISHED // 1000, float(PUBLISHED), float("nan"), float("inf"),
                      "2027-01-03T23:59:00", True, OBSERVED + 1, "not a timestamp"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.normalize(article(publishedAt=value))
        missing = article()
        missing.pop("publishedAt")
        with self.assertRaises(ValueError):
            self.normalize(missing)

    def test_rejects_malicious_urls_without_echoing_raw_input(self):
        invalid = ["javascript:sendOrders()", "https://user:SECRET@example.com/story",
                   "http:///missing", "https://example.com:bad/path", "https://example.com/white space",
                   "https://example.com\r\nHeader:SECRET", "https://[invalid/path", None]
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    self.normalize(article(url=url))
                self.assertNotIn("SECRET", str(caught.exception))

    def test_schema_and_config_errors_are_content_free(self):
        for item in [None, [], article(title=""), article(title="x" * 1001), article(text={"instruction": "BUY"}),
                     article(source={"name": "publisher"}), article(text="x" * 100001)]:
            with self.subTest(item_type=type(item).__name__), self.assertRaises(ValueError):
                self.normalize(item)
        for kwargs in [{"assets": []}, {"assets": ["BTC/EUR"]}, {"assets": [True]},
                       {"observed_at_ms": True}, {"observed_at_ms": 0},
                       {"field_map": {"unknown": "SECRET"}}, {"field_map": {"title": ""}}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.normalize(article(), **kwargs)


class TestNewsMemoryIntegration(unittest.TestCase):
    def test_first_observation_cutoff_and_immutable_reimport(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "news.sqlite3"
            with TradingMemory(path) as memory:
                first = ingest_items(memory, [article()], observed_at_ms=OBSERVED, assets=["BTC"])
                self.assertEqual((first["inserted"], first["duplicates"], first["rejected"]), (1, 0, 0))
                self.assertEqual(memory.recent_news(OBSERVED - 1, "BTC", 300_000), [])
                duplicate = ingest_items(memory, [article(title="Changed later")],
                                         observed_at_ms=OBSERVED + 1000, assets=["BTC"])
                self.assertEqual(duplicate["duplicates"], 1)
            with TradingMemory(path) as memory:
                news = memory.recent_news(OBSERVED + 1000, "BTC", 300_000)
                self.assertEqual(len(news), 1)
                self.assertEqual(news[0]["title"], "A market update")
                self.assertEqual(news[0]["observed_at_ms"], OBSERVED)
                self.assertEqual(memory.recent_news(OBSERVED + 1000, "ETH", 300_000), [])

    def test_valid_records_import_while_bad_rows_have_no_raw_content_in_errors(self):
        with TradingMemory(":memory:") as memory:
            result = ingest_items(memory, [article(), article(url="https://SECRET:SECRET@example.com/a"), 42],
                                  observed_at_ms=OBSERVED, assets=["BTC"])
            self.assertEqual((result["inserted"], result["duplicates"], result["rejected"]), (1, 0, 2))
            self.assertEqual([e["index"] for e in result["errors"]], [1, 2])
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertEqual(len(memory.recent_news(OBSERVED, "BTC", 300_000)), 1)


class TestApifyDatasetReader(unittest.TestCase):
    def request_with_response(self, response, **kwargs):
        with patch("tradingbot.news.urllib.request.build_opener") as factory:
            factory.return_value.open.return_value = io.BytesIO(response)
            result = fetch_apify_dataset("dataset_123", **kwargs)
            request = factory.return_value.open.call_args.args[0]
            self.assertIsInstance(factory.call_args.args[0], _NoRedirect)
            return result, request, factory.return_value.open.call_args.kwargs

    def test_authenticated_read_has_fixed_endpoint_bearer_header_and_bounded_pagination(self):
        result, request, options = self.request_with_response(
            json.dumps([article()]).encode(), token="test-private-token", limit=10, offset=20,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(request.full_url, "https://api.apify.com/v2/datasets/dataset_123/items?format=json&clean=true&limit=10&offset=20&desc=true")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-private-token")
        self.assertNotIn("test-private-token", request.full_url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(options["timeout"], 20)

    def test_public_dataset_needs_no_token_and_redirects_are_disallowed(self):
        result, request, _ = self.request_with_response(b"[]")
        self.assertEqual(result, [])
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(_NoRedirect().redirect_request(request, None, 302, "redirect", {}, "http://evil.example/"))
        _, ascending_request, _ = self.request_with_response(b"[]", descending=False)
        self.assertIn("desc=false", ascending_request.full_url)

    def test_invalid_ids_limits_and_header_injection_are_rejected_before_network(self):
        with patch("tradingbot.news.urllib.request.build_opener") as factory:
            for dataset_id, kwargs in [
                ("../run-actor", {}), ("id?token=SECRET", {}), ("x" * 101, {}),
                ("safe", {"limit": 0}), ("safe", {"limit": 1001}), ("safe", {"limit": True}),
                ("safe", {"offset": -1}), ("safe", {"timeout": float("nan")}),
                ("safe", {"token": "SECRET\r\nHeader:value"}), ("safe", {"descending": "true"}),
            ]:
                with self.subTest(dataset_id=dataset_id, kwargs=kwargs), self.assertRaises(ValueError):
                    fetch_apify_dataset(dataset_id, **kwargs)
            factory.assert_not_called()

    def test_network_errors_do_not_echo_tokens_urls_or_response_bodies(self):
        errors = [
            urllib.error.HTTPError("https://api.apify.com?token=SECRET", 401, "SECRET", {}, None),
            urllib.error.URLError("proxy token=SECRET"), TimeoutError("SECRET"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch("tradingbot.news.urllib.request.build_opener") as factory:
                factory.return_value.open.side_effect = error
                with self.assertRaises(RuntimeError) as caught:
                    fetch_apify_dataset("safe", token="SECRET")
                self.assertNotIn("SECRET", str(caught.exception))

    def test_oversize_invalid_json_and_wrong_shapes_are_rejected(self):
        for body in [b"x" * (MAX_DATASET_BYTES + 1), b"not-json", b"{}", b"[1]", b"[{},{}]"]:
            with self.subTest(length=len(body)), self.assertRaises(RuntimeError):
                self.request_with_response(body, limit=1)


class TestPublicFeedReader(unittest.TestCase):
    def read(self, xml, **kwargs):
        with patch("tradingbot.news.urllib.request.urlopen", return_value=io.BytesIO(xml)) as open_url:
            result = fetch_rss("https://news.example.com/feed", **kwargs)
            request = open_url.call_args.args[0]
            self.assertIsNone(request.get_header("Authorization"))
            return result

    def test_rss_dates_source_and_body_use_common_ingestion_schema(self):
        xml = b'''<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
          <channel><title>News</title><item><title>New event</title>
          <link>https://news.example.com/story#fragment</link>
          <pubDate>Sun, 03 Jan 2027 23:59:00 GMT</pubDate>
          <content:encoded><![CDATA[<p>Report content</p>]]></content:encoded>
          </item></channel></rss>'''
        result = self.read(xml)
        self.assertEqual(len(result), 1)
        normalized = normalize_article(result[0], observed_at_ms=OBSERVED, assets=["BTC"])
        self.assertEqual(normalized["published_at_ms"], PUBLISHED)
        self.assertEqual(normalized["body"], "<p>Report content</p>")
        self.assertEqual(normalized["source"], "news.example.com")
        self.assertEqual(normalized["url"], "https://news.example.com/story")

    def test_atom_namespaces_alternate_link_and_timestamp(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>
          <entry><title>Atom update</title><link rel="self" href="/api/42"/>
          <link rel="alternate" href="/story/42"/><published>2027-01-03T23:59:00Z</published>
          <summary>Event summary</summary></entry></feed>'''
        result = self.read(xml)
        self.assertEqual(result[0]["url"], "https://news.example.com/story/42")
        self.assertEqual(result[0]["text"], "Event summary")
        self.assertEqual(normalize_article(result[0], observed_at_ms=OBSERVED, assets=["BTC"])["published_at_ms"], PUBLISHED)

    def test_missing_or_timezone_naive_dates_are_not_invented_and_limit_is_applied(self):
        xml = b'''<rss><channel>
          <item><title>Undated</title><link>https://example.com/1</link></item>
          <item><title>Naive</title><link>https://example.com/2</link><pubDate>2027-01-03T23:59:00</pubDate></item>
          <item><title>Valid</title><link>https://example.com/3</link><pubDate>Sun, 03 Jan 2027 23:59:00 GMT</pubDate></item>
          <item><title>Extra</title><link>https://example.com/4</link><pubDate>Sun, 03 Jan 2027 23:59:00 GMT</pubDate></item>
        </channel></rss>'''
        result = self.read(xml, limit=1)
        self.assertEqual([item["title"] for item in result], ["Valid"])

    def test_malformed_and_oversize_responses_and_network_errors_are_sanitized(self):
        for raw in (b"not XML", b"<html><body>Access denied</body></html>", b"x" * (MAX_DATASET_BYTES + 1)):
            with self.subTest(length=len(raw)), self.assertRaises(RuntimeError):
                self.read(raw)
        with patch("tradingbot.news.urllib.request.urlopen", side_effect=urllib.error.URLError("SECRET")):
            with self.assertRaises(RuntimeError) as caught:
                fetch_rss("https://news.example.com/feed?secret=SECRET")
            self.assertNotIn("SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
