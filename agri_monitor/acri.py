import logging
import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .models import AcriCreatedEntry, AcriEntry, AcriSyncResult
from .notion import NotionClient

LOG = logging.getLogger(__name__)
NUMBER_RE = re.compile(r"^\d{6}$")


class AcriScrapeError(RuntimeError):
    pass


class AcriScraper:
    def __init__(self, source_url: str, timeout: int = 30):
        self.source_url = source_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "agri-information-monitor/1.0 (+daily public-data monitor)"}
        )

    def _fetch_page(self, page: int) -> str:
        response = self.session.get(
            self.source_url,
            params={
                "Case_Type": "",
                "IsClick": "",
                "KeySrh": "",
                "Sch": "",
                "QNA_No": "",
                "FormGRD_Page": page,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        response.encoding = "big5"
        return response.text

    @staticmethod
    def _max_page(soup: BeautifulSoup) -> int:
        pages = [1]
        for anchor in soup.select("a[href]"):
            values = parse_qs(urlsplit(anchor.get("href", "")).query).get(
                "FormGRD_Page", []
            )
            for value in values:
                if value.isdigit():
                    pages.append(int(value))
        return max(pages)

    def _parse_page(self, html: str, page: int) -> tuple[list[AcriEntry], int]:
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        if len(tables) < 3:
            raise AcriScrapeError(
                f"ACRI 第 {page} 頁結構異常：預期至少 3 個 table，實際 {len(tables)}"
            )

        entries: list[AcriEntry] = []
        for row in tables[2].find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if not cells:
                cells = row.find_all("td")
            if len(cells) < 4:
                continue
            number = cells[0].get_text(" ", strip=True)
            if not NUMBER_RE.fullmatch(number):
                continue
            category = cells[1].get_text(" ", strip=True)
            raw_date = cells[2].get_text(" ", strip=True)
            anchor = cells[3].find("a", href=True)
            question = cells[3].get_text(" ", strip=True)
            if not question or anchor is None:
                raise AcriScrapeError(
                    f"ACRI 第 {page} 頁編號 {number} 缺少問題或明細連結"
                )
            if not category:
                LOG.warning(
                    "ACRI 第 %d 頁編號 %s 的來源類別空白；保留該筆並讓 Notion 類別維持空白",
                    page,
                    number,
                )
            try:
                published_date = datetime.strptime(raw_date, "%Y/%m/%d").date()
            except ValueError as exc:
                raise AcriScrapeError(
                    f"ACRI 第 {page} 頁編號 {number} 日期格式無法辨識：{raw_date}"
                ) from exc
            entries.append(
                AcriEntry(
                    number=number,
                    category=category,
                    published_date=published_date,
                    question=question,
                    source_url=urljoin(self.source_url, anchor["href"]),
                )
            )
        if not entries:
            raise AcriScrapeError(f"ACRI 第 {page} 頁沒有解析到任何有效問答")
        return entries, self._max_page(soup)

    def fetch_all(self) -> list[AcriEntry]:
        first_entries, max_page = self._parse_page(self._fetch_page(1), 1)
        LOG.info("ACRI 動態偵測到 %d 頁", max_page)
        entries = list(first_entries)
        for page in range(2, max_page + 1):
            page_entries, _ = self._parse_page(self._fetch_page(page), page)
            entries.extend(page_entries)

        by_number: dict[str, AcriEntry] = {}
        for entry in entries:
            previous = by_number.get(entry.number)
            if previous and previous != entry:
                raise AcriScrapeError(
                    f"ACRI 來源出現內容不一致的重複編號：{entry.number}"
                )
            by_number.setdefault(entry.number, entry)
        LOG.info("ACRI 全站完成：%d 筆唯一編號", len(by_number))
        return list(by_number.values())


def sync_acri(
    scraper: AcriScraper,
    client: NotionClient,
    *,
    dry_run: bool = False,
) -> AcriSyncResult:
    entries = scraper.fetch_all()
    client.validate_acri_target()
    existing_numbers, duplicate_numbers = client.acri_existing_numbers()
    pending = [entry for entry in entries if entry.number not in existing_numbers]
    LOG.info(
        "ACRI 增量比對：來源 %d 筆、既有編號 %d 筆、待新增 %d 筆",
        len(entries),
        len(existing_numbers),
        len(pending),
    )
    if duplicate_numbers:
        LOG.warning(
            "ACRI Notion 已有重複編號（保留原資料、不再新增）：%s",
            "、".join(duplicate_numbers),
        )

    result = AcriSyncResult(
        discovered_count=len(entries),
        existing_count=len(existing_numbers),
        planned_count=len(pending),
        created=[],
        duplicate_numbers=duplicate_numbers,
    )
    if dry_run or not pending:
        return result

    try:
        client.ensure_acri_categories(
            {entry.category for entry in pending if entry.category}
        )
        for entry in pending:
            page = client.create_acri_page(entry)
            result.created.append(
                AcriCreatedEntry(entry=entry, notion_url=page["url"])
            )
            LOG.info("ACRI 已新增編號 %s：%s", entry.number, entry.question)
    except Exception as exc:
        result.error = str(exc)
        LOG.exception(
            "ACRI 增量同步部分失敗；已成功 %d/%d 筆，下次將依編號續跑：%s",
            len(result.created),
            len(pending),
            exc,
        )
    return result
