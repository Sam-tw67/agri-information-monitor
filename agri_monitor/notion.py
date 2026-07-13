import logging
import time
from collections.abc import Mapping

import requests

from .models import AcriEntry

LOG = logging.getLogger(__name__)
NOTION_VERSION = "2026-03-11"


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(
        self,
        token: str,
        database_id: str,
        data_source_id: str = "",
    ):
        self.database_id = database_id
        self._resolved_data_source_id: str | None = data_source_id or None
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs):
        for attempt in range(6):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < 0.34:
                time.sleep(0.34 - elapsed)
            response = self.session.request(
                method,
                f"https://api.notion.com/v1{path}",
                timeout=30,
                **kwargs,
            )
            self._last_request_at = time.monotonic()
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < 5:
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = min(2 ** attempt, 10)
                    LOG.warning(
                        "Notion API 暫時失敗 %s；%.1f 秒後重試（%d/5）",
                        response.status_code,
                        delay,
                        attempt + 1,
                    )
                    time.sleep(delay)
                    continue
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                detail = response.text or "(Notion 未回傳錯誤內容)"
                raise NotionError(
                    f"Notion API {method} {path} 失敗：{response.status_code} {detail}"
                ) from exc
            return response.json() if response.content else {}
        raise NotionError(f"Notion API {method} {path} 重試後仍失敗")

    def data_source_id(self) -> str:
        """Resolve the single data source contained by the configured database."""
        if self._resolved_data_source_id:
            return self._resolved_data_source_id
        database = self._request("GET", f"/databases/{self.database_id}")
        data_sources = database.get("data_sources", [])
        if len(data_sources) != 1 or not data_sources[0].get("id"):
            raise NotionError(
                "目標 Notion database 必須且只能包含一個 data source；"
                f"目前取得 {len(data_sources)} 個"
            )
        self._resolved_data_source_id = data_sources[0]["id"]
        return self._resolved_data_source_id

    def data_source_schema(self) -> Mapping[str, object]:
        data_source = self._request("GET", f"/data_sources/{self.data_source_id()}")
        properties = data_source.get("properties", {})
        if not isinstance(properties, Mapping):
            raise NotionError("Notion data source 未回傳有效 properties schema")
        return properties

    def _query_all(self, data_source_id: str, payload: dict | None = None) -> list[dict]:
        results: list[dict] = []
        cursor = None
        while True:
            body = dict(payload or {})
            body["page_size"] = 100
            if cursor:
                body["start_cursor"] = cursor
            data = self._request(
                "POST", f"/data_sources/{data_source_id}/query", json=body
            )
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                return results
            cursor = data.get("next_cursor")

    @staticmethod
    def _plain_text(prop: Mapping[str, object], property_type: str) -> str:
        values = prop.get(property_type, [])
        if not isinstance(values, list):
            return ""
        return "".join(str(item.get("plain_text", "")) for item in values).strip()

    def validate_acri_target(self) -> None:
        schema = self.data_source_schema()
        expected = {
            "問題": "title",
            "日期": "date",
            "編號": "rich_text",
            "類別": "select",
        }
        for name, property_type in expected.items():
            prop = schema.get(name)
            if not isinstance(prop, Mapping) or prop.get("type") != property_type:
                raise NotionError(
                    f"ACRI Notion data source 欄位 {name} 必須是 {property_type} 型別"
                )

    def acri_existing_numbers(self) -> tuple[set[str], list[str]]:
        counts: dict[str, int] = {}
        for page in self._query_all(self.data_source_id()):
            properties = page.get("properties", {})
            prop = properties.get("編號", {}) if isinstance(properties, Mapping) else {}
            number = self._plain_text(prop, "rich_text")
            if number:
                counts[number] = counts.get(number, 0) + 1
        duplicates = sorted(number for number, count in counts.items() if count > 1)
        return set(counts), duplicates

    def acri_categories(self) -> list[dict]:
        schema = self.data_source_schema()
        category = schema.get("類別")
        if not isinstance(category, Mapping) or category.get("type") != "select":
            raise NotionError("ACRI Notion data source 缺少 select 型別的 類別 欄位")
        options = category.get("select", {}).get("options", [])
        if not isinstance(options, list):
            raise NotionError("ACRI Notion 類別欄位沒有有效選項清單")
        return options

    def ensure_acri_categories(self, categories: set[str]) -> list[str]:
        options = self.acri_categories()
        existing = {str(option.get("name", "")) for option in options}
        missing = sorted(category for category in categories if category not in existing)
        if not missing:
            return []
        updated_options = [
            {
                "name": str(option.get("name", "")),
                "color": str(option.get("color", "default")),
            }
            for option in options
            if option.get("name")
        ]
        updated_options.extend({"name": name, "color": "default"} for name in missing)
        self._request(
            "PATCH",
            f"/data_sources/{self.data_source_id()}",
            json={"properties": {"類別": {"select": {"options": updated_options}}}},
        )
        LOG.info("ACRI Notion 類別新增選項：%s", "、".join(missing))
        return missing

    def create_acri_page(self, entry: AcriEntry) -> dict:
        properties = {
            "問題": {
                "title": [
                    {
                        "type": "text",
                        "text": {
                            "content": entry.question,
                            "link": {"url": entry.source_url},
                        },
                    }
                ]
            },
            "日期": {"date": {"start": entry.published_date.isoformat()}},
            "編號": {
                "rich_text": [
                    {"type": "text", "text": {"content": entry.number}}
                ]
            },
        }
        if entry.category:
            properties["類別"] = {"select": {"name": entry.category}}
        payload = {
            "parent": {
                "type": "data_source_id",
                "data_source_id": self.data_source_id(),
            },
            "properties": properties,
        }
        page = self._request("POST", "/pages", json=payload)
        if not page.get("id") or not page.get("url"):
            raise NotionError("ACRI Notion 建立頁面成功但未回傳 id 或 url")
        return page
