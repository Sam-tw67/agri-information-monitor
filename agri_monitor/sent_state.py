import json
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .models import Article, Source
from .scraper import normalize_url


STATE_VERSION = 1
DEFAULT_RETENTION_DAYS = 180
TITLE_DATE_PREFIX = re.compile(r"^\s*\d{2,3}[-/]\d{1,2}[-/]\d{1,2}\s+")
PUBLISHER_SUFFIX = re.compile(r"\s+-\s+[^-]{2,80}$")


def _normalized_title(title: str, *, strip_publisher: bool = False) -> str:
    value = TITLE_DATE_PREFIX.sub("", title)
    value = re.sub(r"\s+", " ", value).strip()
    if strip_publisher:
        value = PUBLISHER_SUFFIX.sub("", value).strip()
    return value.casefold()


def _article_url_key(article: Article) -> str:
    return normalize_url(article.canonical_url or article.url)


def _article_signature(article: Article) -> str:
    published = article.published_date.isoformat() if article.published_date else ""
    return _signature(article.title, published, article.canonical_url or article.url)


def _signature(title: str, published: str, url: str) -> str:
    hostname = urlsplit(url).hostname or ""
    normalized_title = _normalized_title(
        title,
        strip_publisher=hostname.lower() == "news.google.com",
    )
    return f"{published}\n{normalized_title}"


@dataclass(frozen=True)
class SentArticleRecord:
    url: str
    title: str
    published_date: date
    source: str
    sent_on: date

    @classmethod
    def from_dict(cls, value: object) -> "SentArticleRecord":
        if not isinstance(value, dict):
            raise RuntimeError("已寄送文章紀錄包含非物件項目")
        try:
            return cls(
                url=str(value["url"]),
                title=str(value["title"]),
                published_date=date.fromisoformat(str(value["published_date"])),
                source=str(value["source"]),
                sent_on=date.fromisoformat(str(value["sent_on"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("已寄送文章紀錄格式不正確") from exc

    def to_dict(self) -> dict[str, str]:
        return {
            "url": self.url,
            "title": self.title,
            "published_date": self.published_date.isoformat(),
            "source": self.source,
            "sent_on": self.sent_on.isoformat(),
        }


class SentArticleState:
    def __init__(self, records: list[SentArticleRecord] | None = None) -> None:
        self.records = records or []
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self._urls = {record.url for record in self.records}
        self._signatures = {
            _signature(record.title, record.published_date.isoformat(), record.url)
            for record in self.records
        }

    @classmethod
    def load(cls, path: Path) -> "SentArticleState":
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"無法讀取已寄送文章紀錄 {path}：{exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            raise RuntimeError(f"已寄送文章紀錄版本不支援：{path}")
        values = payload.get("sent_articles")
        if not isinstance(values, list):
            raise RuntimeError(f"已寄送文章紀錄缺少 sent_articles 陣列：{path}")
        return cls([SentArticleRecord.from_dict(value) for value in values])

    def is_sent(self, article: Article) -> bool:
        return (
            _article_url_key(article) in self._urls
            or _article_signature(article) in self._signatures
        )

    def filter_unsent(
        self, grouped: list[tuple[Source, list[Article]]]
    ) -> list[tuple[Source, list[Article]]]:
        return [
            (source, [article for article in articles if not self.is_sent(article)])
            for source, articles in grouped
        ]

    def record_sent(
        self,
        grouped: list[tuple[Source, list[Article]]],
        sent_on: date,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days 必須大於 0")
        cutoff = sent_on - timedelta(days=retention_days - 1)
        retained = [record for record in self.records if record.published_date >= cutoff]
        by_url = {record.url: record for record in retained}
        for source, articles in grouped:
            for article in articles:
                if article.published_date is None:
                    continue
                url = _article_url_key(article)
                by_url[url] = SentArticleRecord(
                    url=url,
                    title=article.title,
                    published_date=article.published_date,
                    source=source.name,
                    sent_on=sent_on,
                )
        self.records = sorted(
            by_url.values(),
            key=lambda record: (
                record.published_date,
                record.source,
                record.title,
                record.url,
            ),
        )
        self._rebuild_indexes()

    def save(self, path: Path, updated_on: date) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "updated_on": updated_on.isoformat(),
            "sent_articles": [record.to_dict() for record in self.records],
        }
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
