import re

from agri_monitor.config import read_sources


def test_pesticide_title_allowlist_includes_only_requested_categories():
    sources = read_sources("sources.yml")
    pesticide = next(
        source for source in sources if source.name == "農藥與法規修正彙整表"
    )
    patterns = pesticide.include_title_patterns

    included = [
        "公告修正「亞克瑞」農藥使用方法及其範圍如附件",
        "公告「扶比胺」農藥使用方法及其範圍如附件",
        "預先通知公告「派滅芬」農藥使用方法及其範圍",
        "農藥許可證資料更新通知",
    ]
    excluded = [
        "全台各縣市代噴人員登錄名冊",
        "全台各縣市空中施作代噴人員登錄名冊",
        "一般最新消息",
    ]

    assert all(any(re.search(pattern, title) for pattern in patterns) for title in included)
    assert all(not any(re.search(pattern, title) for pattern in patterns) for title in excluded)
