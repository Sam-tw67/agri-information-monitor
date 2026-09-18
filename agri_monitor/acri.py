import html
import logging
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import UUID

import requests
from bs4 import BeautifulSoup

from .models import AcriCreatedEntry, AcriEntry, AcriSyncResult
from .notion import NotionClient

LOG = logging.getLogger(__name__)
NUMBER_RE = re.compile(r"^\d{6}$")
REQUEST_RETRY_DELAYS = (30, 120)
FAQ_PAGE_SIZE = 100
FAQ_CATEGORY_NAMES = {
    "0": "生物農藥",
    "1": "作物健康管理與諮詢",
    "2": "人畜毒性安全",
    "3": "食品安全",
    "4": "藥毒物檢驗",
    "5": "雜草防治",
    "6": "環境安全",
    "7": "農藥調配",
    "8": "農藥管理",
    "9": "農藥毒理與準則",
    "10": "農藥施用技術",
    "11": "農藥品質",
    "12": "農作物污染",
    "13": "無人機藥劑理化性質",
    "14": "教育推廣",
    "15": "除草劑及植物生長調節劑應用",
    "16": "病蟲害防治",
    "17": "非農藥防治資材",
    "18": "水生物與鳥類毒性安全",
    "19": "農藥登記",
    "20": "其他類別",
}


class AcriScrapeError(RuntimeError):
    pass


class AcriScraper:
    def __init__(
        self,
        source_url: str,
        timeout: int = 30,
        retry_delays: tuple[int, ...] = REQUEST_RETRY_DELAYS,
    ):
        self.source_url = source_url
        self.timeout = timeout
        self.retry_delays = retry_delays
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "agri-information-monitor/1.0 (+daily public-data monitor)"}
        )

    def _fetch_page(self, page: int) -> str:
        params = {
            "Case_Type": "",
            "IsClick": "",
            "KeySrh": "",
            "Sch": "",
            "QNA_No": "",
            "FormGRD_Page": page,
        }
        response = self._request("GET", params=params)
        response.encoding = "big5"
        return response.text

    def _request(self, method: str, **kwargs) -> requests.Response:
        request = getattr(self.session, method.lower())
        for attempt in range(len(self.retry_delays) + 1):
            try:
                response = request(
                    self.source_url,
                    timeout=self.timeout,
                    **kwargs,
                )
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                status_code = (
                    exc.response.status_code if exc.response is not None else None
                )
                retryable = isinstance(
                    exc, (requests.ConnectionError, requests.Timeout)
                ) or status_code in {408, 429} or (
                    status_code is not None and 500 <= status_code < 600
                )
                if not retryable or attempt == len(self.retry_delays):
                    raise
                delay = self.retry_delays[attempt]
                LOG.warning(
                    "ACRI 暫時無法連線；%d 秒後重試（%d/%d）：%s",
                    delay,
                    attempt + 1,
                    len(self.retry_delays),
                    exc,
                )
                time.sleep(delay)

        raise AssertionError("ACRI HTTP retry loop ended unexpectedly")

    @staticmethod
    def _clean_question(value: object) -> str:
        question = str(value or "").strip()
        # The new API contains a small number of double-escaped HTML entities.
        return html.unescape(html.unescape(question)).strip()

    def _parse_faq_item(self, item: object) -> AcriEntry:
        if not isinstance(item, dict):
            raise AcriScrapeError("ACRI 新版 FAQ API 回傳非物件項目")
        identifier = str(item.get("FAQId") or "").strip()
        try:
            UUID(identifier)
        except ValueError as exc:
            raise AcriScrapeError(
                f"ACRI 新版 FAQ API 回傳無效 FAQId：{identifier or '(空白)'}"
            ) from exc
        question = self._clean_question(item.get("Question"))
        if not question:
            raise AcriScrapeError(f"ACRI 新版 FAQ {identifier} 缺少問題標題")
        category_id = str(item.get("BusinessCategory") or "").strip()
        if category_id not in FAQ_CATEGORY_NAMES:
            raise AcriScrapeError(
                f"ACRI 新版 FAQ {identifier} 出現未知業務分類：{category_id or '(空白)'}"
            )
        raw_date = str(item.get("PublishDate") or "").strip()
        try:
            published_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date()
        except ValueError as exc:
            raise AcriScrapeError(
                f"ACRI 新版 FAQ {identifier} 日期格式無法辨識：{raw_date or '(空白)'}"
            ) from exc
        # The detail endpoint is POST-only and has no durable public URL, so link
        # to the full mailbox application where readers can open the FAQ list.
        public_url = urljoin(self.source_url, "../../")
        return AcriEntry(
            number=identifier,
            category=FAQ_CATEGORY_NAMES[category_id],
            published_date=published_date,
            question=question,
            source_url=public_url,
        )

    def _fetch_all_faq(self) -> list[AcriEntry]:
        entries: list[AcriEntry] = []
        page = 1
        total: int | None = None
        while total is None or len(entries) < total:
            response = self._request(
                "POST",
                data={
                    "page": page,
                    "pageSize": FAQ_PAGE_SIZE,
                    "skip": (page - 1) * FAQ_PAGE_SIZE,
                    "take": FAQ_PAGE_SIZE,
                },
            )
            try:
                payload = response.json()
            except (requests.JSONDecodeError, ValueError) as exc:
                raise AcriScrapeError(
                    f"ACRI 新版 FAQ API 第 {page} 頁未回傳有效 JSON"
                ) from exc
            data = payload.get("Data") if isinstance(payload, dict) else None
            reported_total = payload.get("Total") if isinstance(payload, dict) else None
            if not isinstance(data, list) or not isinstance(reported_total, int):
                raise AcriScrapeError(
                    f"ACRI 新版 FAQ API 第 {page} 頁缺少 Data 或 Total"
                )
            if total is None:
                total = reported_total
                LOG.info("ACRI 新版 FAQ API 回報 %d 筆", total)
            elif reported_total != total:
                raise AcriScrapeError(
                    f"ACRI 新版 FAQ API 筆數於分頁期間改變：{total} → {reported_total}"
                )
            if not data:
                if len(entries) < total:
                    raise AcriScrapeError(
                        f"ACRI 新版 FAQ API 第 {page} 頁提前回傳空資料"
                    )
                break
            entries.extend(self._parse_faq_item(item) for item in data)
            page += 1
        return entries

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
        if urlsplit(self.source_url).path.rstrip("/").endswith("/GetFaqList"):
            entries = self._fetch_all_faq()
        else:
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
    existing_questions_method = getattr(client, "acri_existing_questions", None)
    existing_questions = (
        existing_questions_method() if callable(existing_questions_method) else set()
    )
    normalized_existing_questions = {
        AcriScraper._clean_question(question) for question in existing_questions
    }
    pending = [
        entry
        for entry in entries
        if entry.number not in existing_numbers
        and AcriScraper._clean_question(entry.question)
        not in normalized_existing_questions
    ]
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
