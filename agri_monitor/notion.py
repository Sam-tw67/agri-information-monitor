import logging
import time
from collections.abc import Mapping

import requests

from .models import AcriEntry, AcriSyncResult, Article, Source

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

    def validate_target(self) -> None:
        """Fail fast on authentication, access, and required schema problems."""
        self._request("GET", "/users/me")
        schema = self.data_source_schema()
        name = schema.get("Name")
        if not isinstance(name, Mapping) or name.get("type") != "title":
            raise NotionError("Notion data source 缺少 title 型別的 Name 欄位")
        status_value = self._status_property(schema)
        status = schema["Status"]
        property_type = status.get("type")
        options = status.get(property_type, {}).get("options", [])
        if options and not any(option.get("name") == "Unread" for option in options):
            raise NotionError("Notion Status 欄位缺少 Unread 選項")
        if not status_value:
            raise NotionError("Notion Status 欄位無法設定 Unread")

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

    def find_page(self, title: str) -> dict | None:
        payload = {
            "filter": {"property": "Name", "title": {"equals": title}},
            "page_size": 2,
        }
        data = self._request(
            "POST",
            f"/data_sources/{self.data_source_id()}/query",
            json=payload,
        )
        results = data.get("results", [])
        if len(results) > 1:
            raise NotionError(f"資料庫已有多筆同名頁面，拒絕任意更新：{title}")
        return results[0] if results else None

    def _status_property(self, schema: Mapping[str, object]) -> dict:
        status = schema.get("Status")
        if not isinstance(status, Mapping):
            raise NotionError("Notion data source 缺少 Status 欄位")
        property_type = status.get("type")
        if property_type == "status":
            return {"status": {"name": "Unread"}}
        if property_type == "select":
            return {"select": {"name": "Unread"}}
        raise NotionError(f"Notion Status 欄位型別不支援：{property_type}")

    def create_page(self, title: str, blocks: list[dict]) -> str:
        schema = self.data_source_schema()
        name = schema.get("Name")
        if not isinstance(name, Mapping) or name.get("type") != "title":
            raise NotionError("Notion data source 缺少 title 型別的 Name 欄位")
        payload = {
            "parent": {
                "type": "data_source_id",
                "data_source_id": self.data_source_id(),
            },
            "properties": {
                "Name": {
                    "title": [
                        {"type": "text", "text": {"content": title}}
                    ]
                },
                "Status": self._status_property(schema),
            },
            "children": blocks[:100],
        }
        page = self._request("POST", "/pages", json=payload)
        if len(blocks) > 100:
            self._append_blocks(page["id"], blocks[100:])
        return page["id"]

    def _children(self, page_id: str) -> list[dict]:
        results = []
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self._request(
                "GET", f"/blocks/{page_id}/children", params=params
            )
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                return results
            cursor = data.get("next_cursor")

    def _append_blocks(self, page_id: str, blocks: list[dict]) -> None:
        for offset in range(0, len(blocks), 100):
            self._request(
                "PATCH",
                f"/blocks/{page_id}/children",
                json={"children": blocks[offset : offset + 100]},
            )

    def replace_content(self, page_id: str, blocks: list[dict]) -> None:
        for block in self._children(page_id):
            self._request("DELETE", f"/blocks/{block['id']}")
        self._append_blocks(page_id, blocks)

    def upsert(self, title: str, blocks: list[dict]) -> str:
        existing = self.find_page(title)
        if existing:
            self.replace_content(existing["id"], blocks)
            LOG.info("已更新既有 Notion page（保留 Status）：%s", title)
            return "updated"
        self.create_page(title, blocks)
        LOG.info("已建立 Notion page：%s", title)
        return "created"


def _rich_text(text: str, url: str | None = None) -> list[dict]:
    return [
        {
            "type": "text",
            "text": {
                "content": text,
                "link": {"url": url} if url else None,
            },
        }
    ]


def _no_update_label(source_name: str) -> str:
    label = source_name.split("－", 1)[0].strip()
    return label.replace("農改場", "改良場")


def _dedupe_labels(labels: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for label in labels:
        if label and label not in seen:
            seen.add(label)
            result.append(label)
    return result


def build_blocks(
    grouped_articles: list[tuple[Source, list[Article]]],
    acri_result: AcriSyncResult | None = None,
) -> list[dict]:
    blocks: list[dict] = []
    if not any(articles for _, articles in grouped_articles) and not any(
        source.show_no_update for source, _ in grouped_articles
    ):
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": _rich_text("本週無符合日期區間的新文章。")
                },
            }
        )
    no_update_labels: list[str] = []
    for source, articles in grouped_articles:
        if not articles:
            if source.show_no_update:
                no_update_labels.append(_no_update_label(source.name))
            continue
        blocks.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": _rich_text(source.name)},
            }
        )
        for article in articles:
            blocks.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": _rich_text(article.title, article.url)
                    },
                }
            )

    acri_blocks: list[dict] = []
    if acri_result is not None:
        if acri_result.created or acri_result.error:
            acri_blocks.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": _rich_text("ACRI 農藥問答集")},
                }
            )
        if acri_result.created:
            for created in acri_result.created:
                acri_blocks.append(
                    {
                        "object": "block",
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {
                            "rich_text": _rich_text(
                                created.entry.question, created.notion_url
                            )
                        },
                    }
                )
        elif acri_result.error is None:
            no_update_labels.append("ACRI 農藥問答集")
        if acri_result.error:
            acri_blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": _rich_text(f"同步失敗：{acri_result.error}")
                    },
                }
            )
        for offset in range(0, len(acri_result.duplicate_numbers), 100):
            numbers = "、".join(acri_result.duplicate_numbers[offset : offset + 100])
            acri_blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": _rich_text(
                            f"警告：ACRI 資料庫已有重複編號（本次未重複新增）：{numbers}"
                        )
                    },
                }
            )
    no_update_labels = _dedupe_labels(no_update_labels)
    if no_update_labels:
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": _rich_text(
                        f"以下監控項目無新增項目來源：{'、'.join(no_update_labels)}。"
                    )
                },
            }
        )
    blocks.extend(acri_blocks)
    return blocks
