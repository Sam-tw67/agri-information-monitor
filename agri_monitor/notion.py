import logging
from collections.abc import Mapping

import requests

from .models import Article, Source

LOG = logging.getLogger(__name__)
NOTION_VERSION = "2022-06-28"


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, token: str, database_id: str):
        self.database_id = database_id
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs):
        response = self.session.request(method, f"https://api.notion.com/v1{path}", timeout=30, **kwargs)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise NotionError(f"Notion API {method} {path} 失敗：{response.status_code} {response.text}") from exc
        return response.json() if response.content else {}

    def database_schema(self) -> Mapping[str, object]:
        return self._request("GET", f"/databases/{self.database_id}").get("properties", {})

    def find_page(self, title: str) -> dict | None:
        payload = {"filter": {"property": "Name", "title": {"equals": title}}, "page_size": 2}
        data = self._request("POST", f"/databases/{self.database_id}/query", json=payload)
        results = data.get("results", [])
        if len(results) > 1:
            raise NotionError(f"資料庫已有多筆同名頁面，拒絕任意更新：{title}")
        return results[0] if results else None

    def _status_property(self, schema: Mapping[str, object]) -> dict:
        status = schema.get("Status")
        if not isinstance(status, Mapping):
            raise NotionError("Notion 資料庫缺少 Status 欄位")
        property_type = status.get("type")
        if property_type == "status":
            return {"status": {"name": "Unread"}}
        if property_type == "select":
            return {"select": {"name": "Unread"}}
        raise NotionError(f"Notion Status 欄位型別不支援：{property_type}")

    def create_page(self, title: str, blocks: list[dict]) -> str:
        schema = self.database_schema()
        name = schema.get("Name")
        if not isinstance(name, Mapping) or name.get("type") != "title":
            raise NotionError("Notion 資料庫缺少 title 型別的 Name 欄位")
        payload = {
            "parent": {"database_id": self.database_id},
            "properties": {
                "Name": {"title": [{"type": "text", "text": {"content": title}}]},
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
            data = self._request("GET", f"/blocks/{page_id}/children", params=params)
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
    return [{"type": "text", "text": {"content": text, "link": {"url": url} if url else None}}]


def build_blocks(grouped_articles: list[tuple[Source, list[Article]]]) -> list[dict]:
    if not any(articles for _, articles in grouped_articles):
        return [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text("本週無符合日期區間的新文章。")} }]
    blocks: list[dict] = []
    for source, articles in grouped_articles:
        if not articles:
            continue
        blocks.append(
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rich_text(source.name)}}
        )
        for article in articles:
            blocks.append(
                {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich_text(article.title, article.url)}}
            )
    return blocks
