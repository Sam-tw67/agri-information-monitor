from agri_monitor.notion import NOTION_VERSION, NotionClient


def make_client(monkeypatch):
    client = NotionClient("token", "database-id")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/databases/database-id":
            return {"data_sources": [{"id": "data-source-id"}]}
        if path == "/data_sources/data-source-id":
            return {
                "properties": {
                    "Name": {"type": "title"},
                    "Status": {"type": "status"},
                }
            }
        if path == "/data_sources/data-source-id/query":
            return {"results": []}
        if path == "/pages":
            return {"id": "new-page-id"}
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


def test_find_page_queries_data_source(monkeypatch):
    client, calls = make_client(monkeypatch)
    assert client.find_page("daily-title") is None
    method, path, kwargs = calls[-1]
    assert method == "POST"
    assert path == "/data_sources/data-source-id/query"
    assert kwargs["json"]["filter"] == {
        "property": "Name",
        "title": {"equals": "daily-title"},
    }


def test_create_page_uses_data_source_parent_and_unread_status(monkeypatch):
    client, calls = make_client(monkeypatch)
    assert client.create_page("daily-title", []) == "new-page-id"
    method, path, kwargs = calls[-1]
    assert method == "POST"
    assert path == "/pages"
    payload = kwargs["json"]
    assert payload["parent"] == {
        "type": "data_source_id",
        "data_source_id": "data-source-id",
    }
    assert payload["properties"]["Status"] == {
        "status": {"name": "Unread"}
    }


def test_validate_target_checks_identity_and_schema(monkeypatch):
    client, calls = make_client(monkeypatch)
    original_request = client._request

    def request(method, path, **kwargs):
        if path == "/users/me":
            calls.append((method, path, kwargs))
            return {"object": "user"}
        return original_request(method, path, **kwargs)

    monkeypatch.setattr(client, "_request", request)
    client.validate_target()
    assert [call[1] for call in calls] == [
        "/users/me",
        "/databases/database-id",
        "/data_sources/data-source-id",
    ]
