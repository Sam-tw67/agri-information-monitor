from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml

from .models import Source


class SourceConfigError(RuntimeError):
    pass


def _read_patterns(item: dict, index: int, key: str) -> tuple[str, ...]:
    raw_patterns = item.get(key, [])
    if not isinstance(raw_patterns, list) or not all(
        isinstance(pattern, str) and pattern.strip() for pattern in raw_patterns
    ):
        raise SourceConfigError(f"sources 第 {index} 筆 {key} 必須是非空字串清單")
    patterns = tuple(pattern.strip() for pattern in raw_patterns)
    try:
        for pattern in patterns:
            re.compile(pattern)
    except re.error as exc:
        raise SourceConfigError(
            f"sources 第 {index} 筆 {key} 不是有效正規表示式：{exc}"
        ) from exc
    return patterns


def read_sources(path: str | Path) -> list[Source]:
    config_path = Path(path)
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SourceConfigError(f"無法讀取來源設定檔 {config_path}：{exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise SourceConfigError(f"來源設定檔 {config_path} 必須包含 sources 清單")

    sources: list[Source] = []
    for index, item in enumerate(data["sources"], start=1):
        if not isinstance(item, dict):
            raise SourceConfigError(f"sources 第 {index} 筆必須是物件")
        if item.get("enabled") is not True:
            continue
        url = str(item.get("url") or "").strip()
        heading = str(
            item.get("output_heading")
            or item.get("notion_heading")
            or item.get("website")
            or ""
        ).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SourceConfigError(f"sources 第 {index} 筆 URL 無效：{url or '(空白)'}")
        if not heading:
            raise SourceConfigError(
                f"sources 第 {index} 筆缺少 output_heading 或 website"
            )
        include_patterns = _read_patterns(item, index, "include_title_patterns")
        exclude_patterns = _read_patterns(item, index, "exclude_title_patterns")
        parser = str(item.get("parser") or "generic").strip()
        if parser not in {"generic", "fda_news_table", "dares_html_list"}:
            raise SourceConfigError(
                f"sources 第 {index} 筆 parser 不支援：{parser}"
            )
        query_keyword = str(item.get("query_keyword") or "").strip()
        latest_only = item.get("latest_only", False)
        show_no_update = item.get("show_no_update", True)
        if not isinstance(latest_only, bool) or not isinstance(show_no_update, bool):
            raise SourceConfigError(
                f"sources 第 {index} 筆 latest_only/show_no_update 必須是布林值"
            )
        sources.append(
            Source(
                name=heading,
                url=url,
                include_title_patterns=include_patterns,
                exclude_title_patterns=exclude_patterns,
                parser=parser,
                query_keyword=query_keyword,
                latest_only=latest_only,
                show_no_update=show_no_update,
            )
        )

    if not sources:
        raise SourceConfigError("來源設定檔沒有任何 enabled: true 的有效來源")
    return sources
