from datetime import date

from agri_monitor.acri import AcriScraper, sync_acri
from agri_monitor.models import AcriEntry, AcriSyncResult
from agri_monitor.notion import NotionClient, build_blocks


def page_html(rows: str, last_page: int = 1) -> str:
    return f"""
    <html><body>
      <table></table><table></table>
      <table>
        <tr><th>編號</th><th>類別</th><th>日期</th><th>問題</th><th>點閱數</th></tr>
        {rows}
      </table>
      <a href="TA02.asp?FormGRD_Page={last_page}#GRD">最後一頁</a>
    </body></html>
    """


def row(number: str, category: str, day: str, question: str) -> str:
    return f"""
      <tr>
        <td>{number}</td><td>{category}</td><td>{day}</td>
        <td><a href="TA03.asp?Enc={number}&amp;Click=Y">{question}</a></td><td>1</td>
      </tr>
    """


def test_acri_parser_reads_number_category_date_question_and_detail_url():
    scraper = AcriScraper("https://mbox.acri.gov.tw/TA02.asp")
    entries, max_page = scraper._parse_page(
        page_html(row("003145", "作物健康管理與諮詢", "2026/06/17", "問題標題"), 34),
        1,
    )
    assert max_page == 34
    assert entries == [
        AcriEntry(
            "003145",
            "作物健康管理與諮詢",
            date(2026, 6, 17),
            "問題標題",
            "https://mbox.acri.gov.tw/TA03.asp?Enc=003145&Click=Y",
        )
    ]


def test_acri_parser_keeps_reliable_entry_when_source_category_is_blank():
    scraper = AcriScraper("https://mbox.acri.gov.tw/TA02.asp")
    entries, _ = scraper._parse_page(
        page_html(row("002933", "", "2024/04/08", "沒有類別的舊問題")), 20
    )
    assert entries[0].number == "002933"
    assert entries[0].category == ""


def test_acri_fetches_dynamic_pages_and_deduplicates_by_number(monkeypatch):
    scraper = AcriScraper("https://mbox.acri.gov.tw/TA02.asp")
    pages = {
        1: page_html(row("000001", "農藥管理", "2026/06/16", "第一題"), 2),
        2: page_html(
            row("000001", "農藥管理", "2026/06/16", "第一題")
            + row("000002", "食品安全", "2026/06/17", "第二題"),
            2,
        ),
    }
    monkeypatch.setattr(scraper, "_fetch_page", lambda page: pages[page])
    assert [entry.number for entry in scraper.fetch_all()] == ["000001", "000002"]


def test_acri_incremental_sync_skips_existing_numbers_and_creates_missing(monkeypatch):
    entries = [
        AcriEntry("000001", "農藥管理", date(2026, 6, 16), "舊題", "https://a/1"),
        AcriEntry("000002", "新類別", date(2026, 6, 17), "新題", "https://a/2"),
    ]

    class Scraper:
        def fetch_all(self):
            return entries

    class Client:
        def validate_acri_target(self):
            pass

        def acri_existing_numbers(self):
            return {"000001"}, ["000001"]

        def ensure_acri_categories(self, categories):
            assert categories == {"新類別"}

        def create_acri_page(self, entry):
            assert entry.number == "000002"
            return {"id": "page-2", "url": "https://notion.test/page-2"}

    result = sync_acri(Scraper(), Client())
    assert result.planned_count == 1
    assert [item.entry.number for item in result.created] == ["000002"]
    assert result.duplicate_numbers == ["000001"]


def test_acri_partial_failure_keeps_successes_for_monitor_page():
    entries = [
        AcriEntry("000001", "農藥管理", date(2026, 6, 16), "第一題", "https://a/1"),
        AcriEntry("000002", "食品安全", date(2026, 6, 17), "第二題", "https://a/2"),
    ]

    class Scraper:
        def fetch_all(self):
            return entries

    class Client:
        def validate_acri_target(self):
            pass

        def acri_existing_numbers(self):
            return set(), []

        def ensure_acri_categories(self, categories):
            pass

        def create_acri_page(self, entry):
            if entry.number == "000002":
                raise RuntimeError("第二筆失敗")
            return {"id": "page-1", "url": "https://notion.test/page-1"}

    result = sync_acri(Scraper(), Client())
    assert [item.entry.number for item in result.created] == ["000001"]
    assert result.error == "第二筆失敗"
    blocks = build_blocks([], result)
    assert "https://notion.test/page-1" in str(blocks)
    assert "同步失敗：第二筆失敗" in str(blocks)


def test_acri_dry_run_never_mutates_notion():
    entry = AcriEntry("000001", "農藥管理", date(2026, 6, 16), "第一題", "https://a/1")

    class Scraper:
        def fetch_all(self):
            return [entry]

    class Client:
        def validate_acri_target(self):
            pass

        def acri_existing_numbers(self):
            return set(), []

        def ensure_acri_categories(self, categories):
            raise AssertionError("dry-run must not update schema")

        def create_acri_page(self, entry):
            raise AssertionError("dry-run must not create page")

    result = sync_acri(Scraper(), Client(), dry_run=True)
    assert result.planned_count == 1
    assert result.created == []


def test_acri_notion_page_title_links_to_original_detail(monkeypatch):
    client = NotionClient("token", "db", "ds")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"id": "page", "url": "https://notion.test/page"}

    monkeypatch.setattr(client, "_request", request)
    entry = AcriEntry("000001", "農藥管理", date(2026, 6, 16), "第一題", "https://a/1")
    client.create_acri_page(entry)
    payload = calls[0][2]["json"]
    assert payload["properties"]["問題"]["title"][0]["text"] == {
        "content": "第一題",
        "link": {"url": "https://a/1"},
    }
    assert payload["properties"]["編號"]["rich_text"][0]["text"]["content"] == "000001"


def test_acri_new_category_is_added_without_removing_existing_options(monkeypatch):
    client = NotionClient("token", "db", "ds")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return {
                "properties": {
                    "類別": {
                        "type": "select",
                        "select": {
                            "options": [{"name": "既有類別", "color": "green"}]
                        },
                    }
                }
            }
        return {}

    monkeypatch.setattr(client, "_request", request)
    assert client.ensure_acri_categories({"既有類別", "新類別"}) == ["新類別"]
    payload = calls[-1][2]["json"]
    assert payload == {
        "properties": {
            "類別": {
                "select": {
                    "options": [
                        {"name": "既有類別", "color": "green"},
                        {"name": "新類別", "color": "default"},
                    ]
                }
            }
        }
    }


def test_acri_monitor_entry_is_only_linked_title():
    entry = AcriEntry("000001", "農藥管理", date(2026, 6, 16), "第一題", "https://a/1")
    from agri_monitor.models import AcriCreatedEntry

    result = AcriSyncResult(
        1, 0, 1, [AcriCreatedEntry(entry, "https://notion.test/page")], []
    )
    blocks = build_blocks([], result)
    item = next(block for block in blocks if block["type"] == "bulleted_list_item")
    assert item["bulleted_list_item"]["rich_text"][0]["text"] == {
        "content": "第一題",
        "link": {"url": "https://notion.test/page"},
    }
    assert "000001" not in str(item)
    assert "2026-06-16" not in str(item)


def test_acri_no_update_uses_monitor_summary_message():
    result = AcriSyncResult(0, 0, 0, [], [])
    blocks = build_blocks([], result)
    serialized = str(blocks)
    assert "以下監控項目無新增項目來源：ACRI 農藥問答集。" in serialized
    assert "本次無新增項目。" not in serialized
    assert all(block["type"] != "heading_2" for block in blocks)
