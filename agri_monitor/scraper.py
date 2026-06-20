import json
import logging
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from .models import Article, Source

LOG = logging.getLogger(__name__)
USER_AGENT = "AgriInformationMonitor/1.0 (+scheduled metadata-only monitor)"
TRACKING_KEYS = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "ref", "source"}
DATE_META_KEYS = {
    "article:published_time", "datepublished", "date", "publishdate", "pubdate",
    "publication_date", "dc.date", "dc.date.issued", "sailthru.date",
}


class SourceFetchError(RuntimeError):
    pass


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    hostname = (parts.hostname or "").lower()
    port = parts.port
    netloc = hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
        )
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            return date.fromisoformat(value.strip())
        return date_parser.parse(str(value)).date()
    except (ValueError, TypeError, OverflowError):
        try:
            return parsedate_to_datetime(str(value)).date()
        except (ValueError, TypeError, OverflowError):
            return None


def _jsonld_articles(value: object):
    if isinstance(value, list):
        for item in value:
            yield from _jsonld_articles(item)
    elif isinstance(value, dict):
        graph = value.get("@graph")
        if graph:
            yield from _jsonld_articles(graph)
        yield value


def extract_page_metadata(html: str, url: str) -> tuple[str | None, date | None, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    title = None
    heading = soup.find("h1")
    if heading:
        title = heading.get_text(" ", strip=True)
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    canonical = None
    canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical_tag and canonical_tag.get("href"):
        canonical = urljoin(url, canonical_tag["href"])
    published = None
    for meta in soup.find_all("meta"):
        key = _normalized_meta_key(meta)
        if key in DATE_META_KEYS:
            published = _parse_date(meta.get("content"))
            if published:
                break
    if not published:
        time_tag = soup.find("time", datetime=True)
        published = _parse_date(time_tag.get("datetime")) if time_tag else None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _jsonld_articles(data):
            item_type = item.get("@type", "")
            types = item_type if isinstance(item_type, list) else [item_type]
            if any(str(value).lower() in {"article", "newsarticle", "blogposting"} for value in types):
                title = title or item.get("headline")
                published = published or _parse_date(item.get("datePublished"))
                canonical = canonical or item.get("url")
    return title, published, canonical


def _normalized_meta_key(meta) -> str:
    return str(meta.get("property") or meta.get("name") or meta.get("itemprop") or "").strip().lower()


def _get(url: str) -> requests.Response:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return response


def _feed_articles(content: bytes, base_url: str) -> list[Article]:
    feed = feedparser.parse(content)
    if not feed.entries:
        return []
    articles = []
    for entry in feed.entries:
        url = entry.get("link")
        title = str(entry.get("title") or "").strip()
        if not url or not title:
            continue
        published = None
        # Atom's ``updated`` is a modification time, not a publication time.
        # Treating it as published would violate the no-date-guessing rule.
        struct_time = entry.get("published_parsed")
        if struct_time:
            published = date(struct_time.tm_year, struct_time.tm_mon, struct_time.tm_mday)
        if not published:
            published = _parse_date(entry.get("published"))
        articles.append(Article(title, urljoin(base_url, url), published))
    return articles


def _feed_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    urls = []
    for link in soup.find_all("link", href=True):
        mime = str(link.get("type") or "").lower()
        if mime in {"application/rss+xml", "application/atom+xml", "application/feed+json"}:
            urls.append(urljoin(base_url, link["href"]))
    return urls


def _html_candidates(html: str, base_url: str) -> list[Article]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[Article] = []
    # A bare ``li`` selector also captures menus and pagination. List entries
    # are article candidates only when the site supplies a publication time.
    containers = soup.select("article, .post, .news, .item, li:has(time)")
    for container in containers:
        link = container.find("a", href=True)
        if not link:
            continue
        url = urljoin(base_url, link["href"])
        if urlsplit(url).scheme not in {"http", "https"}:
            continue
        title = str(link.get("title") or link.get_text(" ", strip=True)).strip()
        if len(title) < 4:
            continue
        time_tag = container.find("time")
        date_value = (time_tag.get("datetime") or time_tag.get_text(" ", strip=True)) if time_tag else None
        if not date_value:
            date_node = container.select_one(".date, .published, .time")
            date_value = date_node.get_text(" ", strip=True) if date_node else None
        candidates.append(Article(title, url, _parse_date(date_value)))
    return candidates


def _pesticide_news_articles(html: str, base_url: str) -> list[Article]:
    soup = BeautifulSoup(html, "html.parser")
    articles = []
    for link in soup.select(".news-list a[href]"):
        title_node = link.select_one(".news-name-long")
        date_node = link.select_one(".news-date")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        published = _parse_date(date_node.get_text(" ", strip=True)) if date_node else None
        if title:
            articles.append(Article(title, urljoin(base_url, link["href"]), published))
    return articles


def fetch_source(source: Source) -> list[Article]:
    try:
        response = _get(source.url)
    except requests.RequestException as exc:
        raise SourceFetchError(f"無法讀取 {source.url}：{exc}") from exc
    content_type = response.headers.get("content-type", "").lower()
    if response.url.startswith("https://pesticide.aphia.gov.tw/information/Data/NewsLast"):
        list_url = urljoin(response.url, "/information/Data/NewsList/?type=new&keyword=&newquery=true")
        try:
            list_response = _get(list_url)
        except requests.RequestException as exc:
            raise SourceFetchError(f"無法讀取農藥最新消息列表 {list_url}：{exc}") from exc
        articles = _pesticide_news_articles(list_response.text, list_response.url)
        if not articles:
            raise SourceFetchError(f"農藥最新消息列表可讀取，但找不到文章：{list_url}")
        return articles
    direct_feed = _feed_articles(response.content, response.url)
    if direct_feed and ("xml" in content_type or b"<rss" in response.content[:1000].lower() or b"<feed" in response.content[:1000].lower()):
        return direct_feed
    soup = BeautifulSoup(response.text, "html.parser")
    for feed_url in _feed_urls(soup, response.url):
        try:
            feed_response = _get(feed_url)
            articles = _feed_articles(feed_response.content, feed_url)
            if articles:
                return articles
        except requests.RequestException as exc:
            LOG.warning("RSS/Atom 讀取失敗，改用 HTML：%s (%s)", feed_url, exc)
    candidates = _html_candidates(response.text, response.url)
    if not candidates:
        LOG.info("來源可讀取但未發現文章候選項目：%s", source.url)
    return candidates


def enrich_article(article: Article) -> Article:
    """Read article metadata only; never retain body, summary, author, image, or tags."""
    try:
        response = _get(article.url)
        page_title, page_date, canonical = extract_page_metadata(response.text, response.url)
        return Article(
            title=article.title or page_title or article.url,
            url=article.url,
            published_date=article.published_date or page_date,
            canonical_url=canonical,
        )
    except requests.RequestException as exc:
        LOG.warning("文章頁無法讀取，僅使用來源提供的可靠資料：%s (%s)", article.url, exc)
        return article


def filter_and_dedupe(articles: list[Article], start_date: date, end_date: date) -> list[Article]:
    result: list[Article] = []
    seen: set[str] = set()
    for article in articles:
        if article.published_date is None:
            LOG.warning("略過無可靠發布日期的文章：%s", article.url)
            continue
        if not start_date <= article.published_date <= end_date:
            continue
        key = normalize_url(article.canonical_url or article.url)
        if key in seen:
            continue
        seen.add(key)
        result.append(article)
    return result
