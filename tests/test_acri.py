from datetime import date
import json

import pytest
import requests

from agri_monitor import acri
from agri_monitor.acri import AcriScraper, sync_acri
from agri_monitor.email_report import build_report_email
from agri_monitor.models import AcriEntry, AcriSyncResult
from agri_monitor.notion import NotionClient


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


def response(status_code: int, text: str = "") -> requests.Response:
    result = requests.Response()
    result.status_code = status_code
    result.url = "https://mbox.acri.gov.tw/TA02.asp"
    result._content = text.encode("big5")
    return result


def json_response(payload: dict) -> requests.Response:
    result = requests.Response()
    result.status_code = 200
    result.url = "https://mboxap.acri.gov.tw/POP30/FAQ/FAQ/GetFaqList"
    result._content = json.dumps(payload).encode("utf-8")
    result.headers["Content-Type"] = "application/json; charset=utf-8"
    return result


def test_acri_fetch_page_retries_temporary_timeout(monkeypatch):
    scraper = AcriScraper(
        "https://mbox.acri.gov.tw/TA02.asp", retry_delays=(30, 120)
    )
    outcomes = [requests.ConnectTimeout("timed out"), response(200, "成功")]
    sleeps = []

    def get(*args, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(scraper.session, "get", get)
    monkeypatch.setattr(acri.time, "sleep", sleeps.append)

    assert scraper._fetch_page(1) == "成功"
    assert sleeps == [30]


def test_acri_fetch_page_does_not_retry_permanent_client_error(monkeypatch):
    scraper = AcriScraper(
        "https://mbox.acri.gov.tw/TA02.asp", retry_delays=(30, 120)
    )
    calls = []
    sleeps = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return response(404)

    monkeypatch.setattr(scraper.session, "get", get)
    monkeypatch.setattr(acri.time, "sleep", sleeps.append)

    with pytest.raises(requests.HTTPError):
        scraper._fetch_page(1)

    assert len(calls) == 1
    assert sleeps == []


def test_acri_new_faq_api_fetches_all_pages_and_maps_fields(monkeypatch):
    source_url = "https://mboxap.acri.gov.tw/POP30/FAQ/FAQ/GetFaqList"
    scraper = AcriScraper(source_url, retry_delays=())
    outcomes = [
        json_response(
            {
                "Data": [
                    {
                        "FAQId": "43441997-5128-43e9-983c-6a205a3db615",
                        "Question": "第一題",
                        "BusinessCategory": "4",
                        "PublishDate": "2026-07-20T00:00:00+00:00",
                    }
                ],
                "Total": 2,
            }
        ),
        json_response(
            {
                "Data": [
                    {
                        "FAQId": "dd84837a-10d9-4c65-a5e6-12f075232e5a",
                        "Question": "第二題含雙重逸出：&amp;#33096;硫",
                        "BusinessCategory": "8",
                        "PublishDate": "2026-07-01T00:00:00Z",
                    }
                ],
                "Total": 2,
            }
        ),
    ]
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return outcomes.pop(0)

    monkeypatch.setattr(scraper.session, "post", post)

    entries = scraper.fetch_all()

    assert entries == [
        AcriEntry(
            "43441997-5128-43e9-983c-6a205a3db615",
            "藥毒物檢驗",
            date(2026, 7, 20),
            "第一題",
            "https://mboxap.acri.gov.tw/POP30/",
        ),
        AcriEntry(
            "dd84837a-10d9-4c65-a5e6-12f075232e5a",
            "農藥管理",
            date(2026, 7, 1),
            "第二題含雙重逸出：腈硫",
            "https://mboxap.acri.gov.tw/POP30/",
        ),
    ]
    assert [call[1]["data"]["page"] for call in calls] == [1, 2]


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


def test_acri_incremental_sync_skips_migrated_question_with_new_identifier():
    entries = [
        AcriEntry(
            "43441997-5128-43e9-983c-6a205a3db615",
            "藥毒物檢驗",
            date(2026, 7, 20),
            "已由舊站匯入的問題",
            "https://new.test/faq",
        ),
        AcriEntry(
            "dd84837a-10d9-4c65-a5e6-12f075232e5a",
            "農藥管理",
            date(2026, 8, 13),
            "新版新增問題",
            "https://new.test/faq",
        ),
    ]

    class Scraper:
        def fetch_all(self):
            return entries

    class Client:
        def validate_acri_target(self):
            pass

        def acri_existing_numbers(self):
            return {"003146"}, []

        def acri_existing_questions(self):
            return {"已由舊站匯入的問題"}

        def ensure_acri_categories(self, categories):
            assert categories == {"農藥管理"}

        def create_acri_page(self, entry):
            assert entry.question == "新版新增問題"
            return {"id": "page", "url": "https://notion.test/page"}

    result = sync_acri(Scraper(), Client())

    assert result.planned_count == 1
    assert [item.entry.question for item in result.created] == ["新版新增問題"]


def test_acri_partial_failure_keeps_successes_for_email_report():
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
    message = build_report_email(
        "日報",
        [],
        result,
        [],
        sender="sender@example.com",
        recipients=("reader@example.com",),
    )
    serialized = (
        message.get_body(preferencelist=("plain",)).get_content()
        + message.get_body(preferencelist=("html",)).get_content()
    )
    assert "https://notion.test/page-1" in serialized
    assert "ACRI 同步失敗：第二筆失敗" in serialized


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


def test_acri_email_entry_is_only_linked_title():
    entry = AcriEntry("000001", "農藥管理", date(2026, 6, 16), "第一題", "https://a/1")
    from agri_monitor.models import AcriCreatedEntry

    result = AcriSyncResult(
        1, 0, 1, [AcriCreatedEntry(entry, "https://notion.test/page")], []
    )
    message = build_report_email(
        "日報",
        [],
        result,
        [],
        sender="sender@example.com",
        recipients=("reader@example.com",),
    )
    html = message.get_body(preferencelist=("html",)).get_content()
    assert '<a href="https://notion.test/page">第一題</a>' in html
    assert "000001" not in html
    assert "2026-06-16" not in html


def test_acri_no_update_uses_email_summary_message():
    result = AcriSyncResult(0, 0, 0, [], [])
    message = build_report_email(
        "日報",
        [],
        result,
        [],
        sender="sender@example.com",
        recipients=("reader@example.com",),
    )
    serialized = message.get_body(preferencelist=("plain",)).get_content()
    assert "以下監控項目無新增項目來源：ACRI 農藥問答集。" in serialized
    assert "本次無新增項目。" not in serialized
