from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    include_title_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    published_date: date | None
    canonical_url: str | None = None
