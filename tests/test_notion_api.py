from agri_monitor.notion import NOTION_VERSION, NotionClient


def make_client(monkeypatch):
    client = NotionClient("token", "database-id")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/databases/database-id":
            return {"data_sources": [{"id": "data-source-id"}]}
        if path == "/data_sources/data-source-id":
            return {"properties": {}}
        raise AssertionError(f"Unexpected request: {method} {path}")

    monkeypatch.setattr(client, "_request", request)
    return client, calls


def test_uses_latest_notion_api_version():
    client = NotionClient("token", "database-id")
    assert NOTION_VERSION == "2026-03-11"
    assert client.session.headers["Notion-Version"] == "2026-03-11"


def test_database_is_resolved_to_single_data_source(monkeypatch):
    client, calls = make_client(monkeypatch)
    assert client.data_source_id() == "data-source-id"
    assert client.data_source_id() == "data-source-id"
    assert [call[1] for call in calls] == ["/databases/database-id"]


def test_explicit_data_source_id_skips_database_discovery(monkeypatch):
    client = NotionClient("token", "database-id", "explicit-data-source-id")
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("database discovery must not run")
        ),
    )
    assert client.data_source_id() == "explicit-data-source-id"


def test_acri_existing_scan_collects_identifiers_and_questions(monkeypatch):
    client = NotionClient("token", "database-id", "data-source-id")
    pages = [
        {
            "properties": {
                "編號": {"rich_text": [{"plain_text": "003146"}]},
                "問題": {"title": [{"plain_text": "既有問題"}]},
            }
        },
        {
            "properties": {
                "編號": {"rich_text": [{"plain_text": "003146"}]},
                "問題": {"title": [{"plain_text": "另一個問題"}]},
            }
        },
    ]
    monkeypatch.setattr(client, "_query_all", lambda _data_source_id: pages)

    numbers, duplicates = client.acri_existing_numbers()

    assert numbers == {"003146"}
    assert duplicates == ["003146"]
    assert client.acri_existing_questions() == {"既有問題", "另一個問題"}
