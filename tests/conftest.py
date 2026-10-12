import pytest


@pytest.fixture(autouse=True)
def isolated_article_state(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTICLE_STATE_FILE", str(tmp_path / "sent_articles.json"))
    monkeypatch.setenv("ARTICLE_LOOKBACK_DAYS", "30")
