"""Safe Notion API transport probe: prints metadata, never credentials or content."""

import os

import requests


TOKEN = os.environ["NOTION_TOKEN"].strip()
DATA_SOURCE_ID = os.environ["NOTION_DATA_SOURCE_ID"].strip()


def probe(version: str, label: str, method: str, path: str, payload=None) -> None:
    try:
        response = requests.request(
            method,
            f"https://api.notion.com/v1{path}",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        print(
            f"version={version} check={label} status={response.status_code} "
            f"content_type={response.headers.get('content-type', '(none)')} "
            f"body_bytes={len(response.content)} "
            f"request_id_present={bool(response.headers.get('x-notion-request-id'))}"
        )
    except requests.RequestException as exc:
        print(f"version={version} check={label} transport_error={type(exc).__name__}")


print(f"token_length={len(TOKEN)} token_ascii={TOKEN.isascii()}")
for api_version in ("2025-09-03", "2026-03-11"):
    probe(api_version, "users_me", "GET", "/users/me")
    probe(
        api_version,
        "data_source_get",
        "GET",
        f"/data_sources/{DATA_SOURCE_ID}",
    )
    probe(
        api_version,
        "query_empty",
        "POST",
        f"/data_sources/{DATA_SOURCE_ID}/query",
        {},
    )
    probe(
        api_version,
        "query_name_filter",
        "POST",
        f"/data_sources/{DATA_SOURCE_ID}/query",
        {
            "filter": {
                "property": "Name",
                "title": {"equals": "diagnostic-title-that-must-not-exist"},
            },
            "page_size": 2,
        },
    )
