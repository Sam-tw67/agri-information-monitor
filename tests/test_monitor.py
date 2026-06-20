from datetime import date

import pytest

from agri_monitor import app
from agri_monitor.config import read_sources
from agri_monitor.dates import monitoring_window, page_title
from agri_monitor.models import AcriSyncResult, Article, Source
from agri_monitor.notion import NotionClient, build_blocks
from agri_monitor.scraper import _html_candidates, _pesticide_news_articles, filter_and_dedupe


def test_monitoring_window_is_previous_seven_calendar_days():
    assert monitoring_window(date(2026, 6, 22)) == (date(2026, 6, 15), date(2026, 6, 21))


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
    assert page_title(date(2026, 6, 15), date(2026, 6, 21)) == (
        "農業資訊監控排程任務 (上次:2026-06-15/ 本次:2026-06-21)"
    )


def test_notion_blocks_only_contain_linked_title_not_body_or_summary():
    blocks = build_blocks(
        [(Source("來源 A", "https://source.test"), [Article("文章標題", "https://example.com/a", date(2026, 6, 20))])]
    )
    item = blocks[1]["bulleted_list_item"]["rich_text"][0]
    assert item["text"] == {"content": "文章標題", "link": {"url": "https://example.com/a"}}
    serialized = str(blocks)
    assert "summary" not in serialized.lower()
    assert "body" not in serialized.lower()
    assert "2026-06-20" not in serialized


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
    monkeypatch.setattr(app, "fetch_source", lambda source: [Article("T", "https://article.test", date(2026, 6, 20))])
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
    assert "農業資訊監控排程任務 (上次:2026-06-15/ 本次:2026-06-21)" in output
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


def test_acri_failure_updates_weekly_report_then_marks_run_failed(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "secret")
    monkeypatch.setenv("NOTION_DATABASE_ID", "weekly-db")
    monkeypatch.setenv("ACRI_NOTION_DATABASE_ID", "acri-db")
    monkeypatch.setenv("ACRI_SOURCE_URL", "https://acri.test/TA02.asp")
    monkeypatch.setattr(app, "read_sources", lambda *args: [Source("A", "https://source.test")])
    monkeypatch.setattr(
        app,
        "fetch_source",
        lambda source: [Article("T", "https://article.test", date(2026, 6, 20))],
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
