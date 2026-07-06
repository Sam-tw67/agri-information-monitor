from datetime import date

import pytest

from agri_monitor import app
from agri_monitor.config import read_sources
from agri_monitor.dates import monitoring_window, page_title
from agri_monitor.models import AcriSyncResult, Article, Source
from agri_monitor.notion import NotionClient, build_blocks
from agri_monitor.scraper import (
    _dares_html_list_articles,
    _feed_articles,
    _fda_news_articles,
    _html_candidates,
    _pesticide_news_articles,
    filter_and_dedupe,
)


def test_monitoring_window_is_previous_calendar_day():
    assert monitoring_window(date(2026, 6, 22)) == (date(2026, 6, 21), date(2026, 6, 21))


def test_filter_is_inclusive_and_excludes_outside_dates():
    articles = [
        Article("start", "https://example.com/start", date(2026, 6, 15)),
        Article("end", "https://example.com/end", date(2026, 6, 21)),
        Article("before", "https://example.com/before", date(2026, 6, 14)),
        Article("after", "https://example.com/after", date(2026, 6, 22)),
    ]
    result = filter_and_dedupe(articles, date(2026, 6, 15), date(2026, 6, 21))
    assert [article.title for article in result] == ["start", "end"]


def test_missing_date_is_excluded():
    result = filter_and_dedupe(
        [Article("unknown", "https://example.com/unknown", None)],
        date(2026, 6, 15),
        date(2026, 6, 21),
    )
    assert result == []


def test_url_deduplication_uses_normalized_or_canonical_url():
    articles = [
        Article("one", "https://EXAMPLE.com/a/?utm_source=x#top", date(2026, 6, 16)),
        Article("two", "https://example.com/a", date(2026, 6, 17)),
        Article("three", "https://elsewhere.test/x", date(2026, 6, 18), "https://example.com/a/"),
    ]
    result = filter_and_dedupe(articles, date(2026, 6, 15), date(2026, 6, 21))
    assert [article.title for article in result] == ["one"]


def test_page_title_exact_format():
    assert page_title(date(2026, 6, 21), date(2026, 6, 21)) == (
        "農業資訊每日監控 (日期:2026-06-21)"
    )


def test_notion_blocks_only_contain_linked_title_not_body_or_summary():
    blocks = build_blocks(
        [(Source("來源 A", "https://source.test"), [Article("文章標題", "https://example.com/a", date(2026, 6, 21))])]
    )
    item = blocks[1]["bulleted_list_item"]["rich_text"][0]
    assert item["text"] == {"content": "文章標題", "link": {"url": "https://example.com/a"}}
    serialized = str(blocks)
    assert "summary" not in serialized.lower()
    assert "body" not in serialized.lower()
    assert "2026-06-21" not in serialized


def test_upsert_updates_existing_page_and_preserves_status(monkeypatch):
    client = object.__new__(NotionClient)
    monkeypatch.setattr(client, "find_page", lambda title: {"id": "existing-page"})
    calls = []
    monkeypatch.setattr(client, "replace_content", lambda page_id, blocks: calls.append((page_id, blocks)))
    monkeypatch.setattr(client, "create_page", lambda *_: pytest.fail("must not create duplicate page"))
    assert client.upsert("same-title", [{"block": 1}]) == "updated"
    assert calls == [("existing-page", [{"block": 1}])]


def test_dry_run_reads_notion_but_does_not_write(monkeypatch, capsys):
    monkeypatch.setenv("NOTION_TOKEN", "secret")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db")
    monkeypatch.setenv("ACRI_NOTION_DATABASE_ID", "acri-db")
    monkeypatch.setenv("ACRI_SOURCE_URL", "https://acri.test/TA02.asp")
    monkeypatch.setattr(app, "read_sources", lambda *args: [Source("A", "https://source.test")])
    monkeypatch.setattr(app, "fetch_source", lambda source: [Article("T", "https://article.test", date(2026, 6, 21))])
    monkeypatch.setattr(app, "enrich_article", lambda article: article)
    clients = []

    class FakeClient:
        def __init__(self, *args):
            clients.append(self)

        def validate_target(self):
            pass

        def upsert(self, *_):
            pytest.fail("dry-run must not write Notion")

    monkeypatch.setattr(app, "NotionClient", FakeClient)
    monkeypatch.setattr(
        app,
        "sync_acri",
        lambda *args, **kwargs: AcriSyncResult(10, 8, 2, [], []),
    )
    assert app.run(date(2026, 6, 22), dry_run=True) == 0
    output = capsys.readouterr().out
    assert "農業資訊每日監控 (日期:2026-06-21)" in output
    assert "articles=1" in output
    assert "acri_new=2" in output
    assert len(clients) == 2


def test_source_config_uses_urls_and_notion_headings(tmp_path):
    config = tmp_path / "sources.yml"
    config.write_text(
        """sources:
  - website: 上下游新聞
    url: https://www.newsmarket.com.tw/
    notion_heading: 上下游
    enabled: true
  - website: 農藥資訊服務網
    url: https://pesticide.aphia.gov.tw/information/Data/NewsLast
    notion_heading: 農藥與法規修正彙整表
    enabled: true
  - website: 疫情預警
    url: https://phis.aphia.gov.tw/list-1-102
    notion_heading: 植物疫情彙整表
    enabled: true
""",
        encoding="utf-8",
    )
    sources = read_sources(config)
    assert [(source.name, source.url) for source in sources] == [
        ("上下游", "https://www.newsmarket.com.tw/"),
        ("農藥與法規修正彙整表", "https://pesticide.aphia.gov.tw/information/Data/NewsLast"),
        ("植物疫情彙整表", "https://phis.aphia.gov.tw/list-1-102"),
    ]
    assert all(source.show_no_update for source in sources)


def test_hdares_sources_are_official_rss_feeds():
    sources = read_sources("sources.yml")
    hdares = [source for source in sources if source.name.startswith("花蓮農改場")]
    assert [(source.name, source.url) for source in hdares] == [
        (
            "花蓮農改場－本場新聞",
            "https://www.hdares.gov.tw/api.php?func=news&format=rss",
        ),
        (
            "花蓮農改場－最新消息",
            "https://www.hdares.gov.tw/api.php?func=hotnews&format=rss",
        ),
        (
            "花蓮農改場－近期活動",
            "https://www.hdares.gov.tw/api.php?func=activity&format=rss",
        ),
    ]


def test_new_dares_sources_are_configured_for_daily_monitoring():
    sources = read_sources("sources.yml")
    selected = [
        (source.name, source.url, source.parser)
        for source in sources
        if source.name.startswith((
            "桃園農改場",
            "苗栗農改場",
            "台中農改場",
            "高雄農改場",
            "台南農改場",
            "台東農改場",
        ))
    ]
    assert selected == [
        (
            "桃園農改場－活動訊息",
            "https://www.tydares.gov.tw/theme_list.php?theme=activity&sub_theme=",
            "dares_html_list",
        ),
        (
            "苗栗農改場－農業新聞",
            "https://www.mdares.gov.tw/theme_list.php?theme=news&sub_theme=agri_news",
            "dares_html_list",
        ),
        (
            "苗栗農改場－最新消息",
            "https://www.mdares.gov.tw/theme_list.php?theme=hotnews_ws",
            "dares_html_list",
        ),
        (
            "台中農改場－新聞資訊",
            "https://www.tcdares.gov.tw/api.php?theme=news&sub_theme=news&format=rss",
            "generic",
        ),
        (
            "台中農改場－最新消息",
            "https://www.tcdares.gov.tw/api.php?theme=news&sub_theme=hot&format=rss",
            "generic",
        ),
        (
            "高雄農改場－公告資訊",
            "https://www.kdais.gov.tw/theme_list.php?theme=news&sub_theme=announcement",
            "dares_html_list",
        ),
        (
            "台南農改場－本場快訊",
            "https://www.tndais.gov.tw/theme_list.php?theme=news_list",
            "dares_html_list",
        ),
        (
            "台東農改場－新聞",
            "https://www.ttdares.gov.tw/theme_list.php?theme=news&sub_theme=news",
            "dares_html_list",
        ),
    ]


def test_source_config_reads_exclude_title_patterns():
    sources = read_sources("sources.yml")
    by_name = {source.name: source for source in sources}
    assert "協助.*公告" in by_name["台中農改場－最新消息"].exclude_title_patterns
    assert "營養午餐|午餐法" in by_name["上下游"].exclude_title_patterns
    assert by_name["農藥與法規修正彙整表"].exclude_title_patterns == ()
    assert by_name["植物疫情彙整表"].exclude_title_patterns == ()


def test_source_title_exclude_patterns_remove_unwanted_categories():
    source = Source(
        "台中農改場－最新消息",
        "https://www.tcdares.gov.tw/api.php?theme=news&sub_theme=hot&format=rss",
        exclude_title_patterns=("協助.*公告", "職缺|徵才|約用人員"),
    )
    articles = [
        Article("協助農業部公告115年度「AXIS－農業跨域數位領袖班」", "https://example.test/a", date(2026, 6, 22)),
        Article("公告本場農業推廣科約用人員職缺", "https://example.test/b", date(2026, 6, 25)),
        Article("豪雨後，臺中農改場籲請農友儘速做好作物復耕復育措施!", "https://example.test/c", date(2026, 6, 29)),
    ]
    assert app._apply_source_title_filters(source, articles) == [articles[2]]


def test_run_level_dedupe_skips_same_day_same_site_same_title():
    articles = [
        Article(
            "豪雨後，臺中農改場籲請農友儘速做好作物復耕復育措施!",
            "https://www.tcdares.gov.tw/theme_data.php?theme=news&sub_theme=news&id=16193",
            date(2026, 6, 29),
        ),
        Article(
            "豪雨後，臺中農改場籲請農友儘速做好作物復耕復育措施!",
            "https://www.tcdares.gov.tw/theme_data.php?theme=news&sub_theme=hot&id=16193",
            date(2026, 6, 29),
        ),
        Article(
            "豪雨後，臺中農改場籲請農友儘速做好作物復耕復育措施!",
            "https://elsewhere.test/theme_data.php?id=16193",
            date(2026, 6, 29),
        ),
    ]
    assert app._dedupe_run_articles(articles, set(), set()) == [articles[0], articles[2]]


def test_acri_failure_updates_monitor_page_then_marks_run_failed(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "secret")
    monkeypatch.setenv("NOTION_DATABASE_ID", "monitor-db")
    monkeypatch.setenv("ACRI_NOTION_DATABASE_ID", "acri-db")
    monkeypatch.setenv("ACRI_SOURCE_URL", "https://acri.test/TA02.asp")
    monkeypatch.setattr(app, "read_sources", lambda *args: [Source("A", "https://source.test")])
    monkeypatch.setattr(
        app,
        "fetch_source",
        lambda source: [Article("T", "https://article.test", date(2026, 6, 21))],
    )
    monkeypatch.setattr(app, "enrich_article", lambda article: article)
    writes = []

    class FakeClient:
        def __init__(self, token, database_id, data_source_id=""):
            self.database_id = database_id

        def validate_target(self):
            pass

        def upsert(self, title, blocks):
            writes.append((title, blocks))
            return "updated"

    monkeypatch.setattr(app, "NotionClient", FakeClient)
    monkeypatch.setattr(
        app,
        "sync_acri",
        lambda *args, **kwargs: AcriSyncResult(
            0, 0, 0, [], [], error="ACRI 測試錯誤"
        ),
    )
    with pytest.raises(RuntimeError, match="ACRI 同步失敗"):
        app.run(date(2026, 6, 22))
    assert len(writes) == 1
    assert "同步失敗：ACRI 測試錯誤" in str(writes[0][1])


def test_feed_parser_extracts_only_title_url_and_date():
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><item>
      <title>花蓮農改場公告標題</title>
      <link>https://www.hdares.gov.tw/theme_data.php?id=1</link>
      <description><![CDATA[
        <p>第一行內容</p>
        <p>第二行 <strong>重點</strong></p>
      ]]></description>
      <pubDate>Sun, 21 Jun 2026 00:00:00 +0800</pubDate>
    </item></channel></rss>
    """.encode("utf-8")
    articles = _feed_articles(
        rss,
        "https://www.hdares.gov.tw/api.php?func=news&format=rss",
    )
    assert articles == [
        Article(
            "花蓮農改場公告標題",
            "https://www.hdares.gov.tw/theme_data.php?id=1",
            date(2026, 6, 21),
        )
    ]


def test_dares_html_list_parser_extracts_roc_dates_titles_and_urls():
    html = """
    <div class="trs">
      <div class="tds"><span class="color_green">115-06-24</span></div>
      <div class="tds">
        <a href="theme_data.php?theme=activity&amp;id=1" title="北部地區作物關鍵害物防治技術與產業應用研討會" class="links">
          北部地區作物關鍵害物防治技術與產業應用研討會
        </a>
      </div>
    </div>
    <a href="/theme_data.php?theme=hotnews_ws&amp;id=2" target="_self" title="嚴正聲明：本場無販售農產品">
      <div class="date">115-05-09</div>
      <div class="txt">嚴正聲明：本場無販售農產品</div>
    </a>
    <a href="/theme_list.php?theme=news&amp;sub_theme=agri_news">了解更多</a>
    """
    articles = _dares_html_list_articles(html, "https://www.tydares.gov.tw/theme_list.php?theme=activity")
    assert articles == [
        Article(
            "北部地區作物關鍵害物防治技術與產業應用研討會",
            "https://www.tydares.gov.tw/theme_data.php?theme=activity&id=1",
            date(2026, 6, 24),
        ),
        Article(
            "嚴正聲明：本場無販售農產品",
            "https://www.tydares.gov.tw/theme_data.php?theme=hotnews_ws&id=2",
            date(2026, 5, 9),
        ),
    ]


def test_phis_list_uses_time_text_and_ignores_navigation_links():
    html = """
    <nav><li><a href="/list-1-149">導覽選單項目</a></li></nav>
    <ul><li><a href="article-1-102-123" title="疫情預警標題">
      疫情預警標題<time>2026-06-15</time></a></li></ul>
    """
    articles = _html_candidates(html, "https://phis.aphia.gov.tw/list-1-102")
    assert articles == [
        Article("疫情預警標題", "https://phis.aphia.gov.tw/article-1-102-123", date(2026, 6, 15))
    ]


def test_pesticide_dynamic_list_parser_extracts_only_title_url_and_date():
    html = """
    <div class="news-list">
      <a href="/information/Data/NewsContent/3288">
        <span class="news-date">2026-06-18</span>
        <span class="news-name-long">全台各縣市代噴人員登錄名冊</span>
      </a>
    </div>
    """
    articles = _pesticide_news_articles(html, "https://pesticide.aphia.gov.tw/information/Data/NewsList/")
    assert articles == [
        Article(
            "全台各縣市代噴人員登錄名冊",
            "https://pesticide.aphia.gov.tw/information/Data/NewsContent/3288",
            date(2026, 6, 18),
        )
    ]


def test_fda_parser_extracts_publication_date_and_original_url():
    html = """
    <table class="listTable"><tbody>
      <tr><th>序號</th><th>標題</th><th>發布日期</th></tr>
      <tr><td>1</td><td><a href="newsContent.aspx?cid=3&amp;id=31518">
        修正「農藥殘留容許量標準」第三條附表一
      </a></td><td>2026-04-21</td></tr>
    </tbody></table>
    """
    assert _fda_news_articles(html, "https://www.fda.gov.tw/TC/news.aspx?cid=3") == [
        Article(
            "修正「農藥殘留容許量標準」第三條附表一",
            "https://www.fda.gov.tw/TC/newsContent.aspx?cid=3&id=31518",
            date(2026, 4, 21),
        )
    ]


def test_latest_only_selects_newest_reliable_matching_announcement():
    articles = [
        Article("舊版", "https://fda.test/old", date(2025, 12, 1)),
        Article("最新版", "https://fda.test/new", date(2026, 4, 21)),
        Article("無日期", "https://fda.test/unknown", None),
    ]
    assert app._latest_reliable_article("衛福部食藥署", articles).title == "最新版"


def test_source_with_no_update_gets_single_summary_message():
    source = Source(
        "衛福部食藥署",
        "https://www.fda.gov.tw/TC/news.aspx?cid=3",
        show_no_update=True,
    )
    blocks = build_blocks([(source, [])])
    assert "衛福部食藥署" in str(blocks)
    assert "以下監控項目無新增項目來源：衛福部食藥署。" in str(blocks)
    assert "本次無新增項目。" not in str(blocks)
    assert all(block["type"] != "heading_2" for block in blocks)


def test_all_empty_sources_are_collapsed_into_one_no_update_summary():
    sources = [
        Source("花蓮農改場－本場新聞", "https://www.hdares.gov.tw/api.php?func=news&format=rss"),
        Source("花蓮農改場－最新消息", "https://www.hdares.gov.tw/api.php?func=hotnews&format=rss"),
        Source("台中農改場－新聞資訊", "https://www.tcdares.gov.tw/api.php?theme=news&sub_theme=news&format=rss"),
        Source("上下游", "https://www.newsmarket.com.tw/"),
    ]
    blocks = build_blocks([(source, []) for source in sources])
    serialized = str(blocks)
    assert "以下監控項目無新增項目來源：花蓮改良場、台中改良場、上下游。" in serialized
    assert serialized.count("以下監控項目無新增項目來源") == 1
    assert "花蓮改良場" in serialized
    assert "花蓮農改場－本場新聞" not in serialized
    assert "花蓮農改場－最新消息" not in serialized
    assert "本次無新增項目。" not in serialized
    assert "本週無符合日期區間的新文章。" not in serialized
