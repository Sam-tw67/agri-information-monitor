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


def test_fda_source_tracks_only_latest_pesticide_mrl_standard():
    sources = read_sources("sources.yml")
    fda = next(source for source in sources if source.name == "衛福部食藥署")
    assert fda.url == "https://www.fda.gov.tw/TC/news.aspx?cid=3"
    assert fda.query_keyword == "農藥殘留容許量標準"
    assert fda.parser == "fda_news_table"
    assert fda.latest_only is True
    assert fda.show_no_update is True
    assert any(
        re.search(pattern, "修正「農藥殘留容許量標準」第三條附表一")
        for pattern in fda.include_title_patterns
    )
