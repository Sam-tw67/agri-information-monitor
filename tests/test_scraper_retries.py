from datetime import date

import requests
import pytest

from agri_monitor import scraper
from agri_monitor.models import Article, Source


def _response(status_code: int, url: str = "https://source.test/") -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.url = url
    return response


def test_get_retries_connection_errors_with_delays(monkeypatch):
    outcomes = [
        requests.ConnectionError("network unreachable"),
        _response(200),
    ]
    calls = []
    sleeps = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(scraper.requests, "get", fake_get)
    monkeypatch.setattr(scraper.time, "sleep", sleeps.append)

    assert scraper._get("https://source.test/").status_code == 200
    assert len(calls) == 2
    assert sleeps == [5]


def test_get_retries_once_then_raises(monkeypatch):
    calls = []
    sleeps = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        raise requests.ConnectionError("network unreachable")

    monkeypatch.setattr(scraper.requests, "get", fake_get)
    monkeypatch.setattr(scraper.time, "sleep", sleeps.append)

    with pytest.raises(requests.ConnectionError, match="network unreachable"):
        scraper._get("https://source.test/")

    assert len(calls) == 2
    assert sleeps == [5]


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
def test_get_retries_retryable_http_statuses(monkeypatch, status_code):
    responses = [_response(status_code), _response(200)]
    sleeps = []

    monkeypatch.setattr(scraper.requests, "get", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(scraper.time, "sleep", sleeps.append)

    assert scraper._get("https://source.test/").status_code == 200
    assert sleeps == [5]


def test_get_does_not_retry_other_client_errors(monkeypatch):
    calls = []
    sleeps = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return _response(404)

    monkeypatch.setattr(scraper.requests, "get", fake_get)
    monkeypatch.setattr(scraper.time, "sleep", sleeps.append)

    with pytest.raises(requests.HTTPError):
        scraper._get("https://source.test/")

    assert len(calls) == 1
    assert sleeps == []


def test_wordpress_posts_source_uses_curl_and_parses_entries(monkeypatch):
    source = Source(
        "上下游",
        "https://www.newsmarket.com.tw/?rest_route=/wp/v2/posts&per_page=30",
        parser="wordpress_posts_json",
    )
    content = b'''[
      {"date":"2026-07-30T20:56:42", "link":"https://www.newsmarket.com.tw/blog/1/", "title":{"rendered":"\xe6\xb8\xac\xe8\xa9\xa6\xe6\xa8\x99\xe9\xa1\x8c"}}
    ]'''
    requested_urls = []
    monkeypatch.setattr(
        scraper,
        "_curl_get",
        lambda url: requested_urls.append(url) or content,
    )
    monkeypatch.setattr(
        scraper.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("WordPress JSON 不應使用 requests"),
    )

    assert scraper.fetch_source(source) == [
        Article("測試標題", "https://www.newsmarket.com.tw/blog/1/", date(2026, 7, 30))
    ]
    assert requested_urls == [source.url]


def test_wordpress_posts_source_uses_configured_rss_fallback_after_curl_403(monkeypatch):
    source = Source(
        "上下游",
        "https://www.newsmarket.com.tw/?rest_route=/wp/v2/posts&per_page=30",
        parser="wordpress_posts_json",
        fallback_url="https://news.google.com/rss/search?q=site%3Anewsmarket.com.tw",
    )
    response = _response(200, source.fallback_url)
    response._content = b"""<?xml version=\"1.0\"?><rss><channel><item>
      <title>\xe4\xb8\x8a\xe4\xb8\x8b\xe6\xb8\xb8\xe5\x82\x99\xe6\x8f\xb4\xe6\xa8\x99\xe9\xa1\x8c</title>
      <link>https://news.google.com/rss/articles/example</link>
      <pubDate>Thu, 30 Jul 2026 03:00:00 +0800</pubDate>
    </item></channel></rss>"""
    monkeypatch.setattr(
        scraper,
        "_curl_get",
        lambda _url: (_ for _ in ()).throw(scraper.SourceFetchError("curl: (22) 403")),
    )
    monkeypatch.setattr(scraper, "_get", lambda _url: response)

    assert scraper.fetch_source(source) == [
        Article(
            "上下游備援標題",
            "https://news.google.com/rss/articles/example",
            date(2026, 7, 30),
        )
    ]
