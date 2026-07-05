from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    include_title_patterns: tuple[str, ...] = ()
    exclude_title_patterns: tuple[str, ...] = ()
    parser: str = "generic"
    query_keyword: str = ""
    latest_only: bool = False
    show_no_update: bool = True


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    published_date: date | None
    canonical_url: str | None = None


@dataclass(frozen=True)
class AcriEntry:
    number: str
    category: str
    published_date: date
    question: str
    source_url: str


@dataclass(frozen=True)
class AcriCreatedEntry:
    entry: AcriEntry
    notion_url: str


@dataclass
class AcriSyncResult:
    discovered_count: int
    existing_count: int
    planned_count: int
    created: list[AcriCreatedEntry]
    duplicate_numbers: list[str]
    error: str | None = None
