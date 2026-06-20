from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .models import Source


class SourceConfigError(RuntimeError):
    pass


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
        heading = str(item.get("notion_heading") or item.get("website") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SourceConfigError(f"sources 第 {index} 筆 URL 無效：{url or '(空白)'}")
        if not heading:
            raise SourceConfigError(f"sources 第 {index} 筆缺少 notion_heading 或 website")
        sources.append(Source(name=heading, url=url))

    if not sources:
        raise SourceConfigError("來源設定檔沒有任何 enabled: true 的有效來源")
    return sources
