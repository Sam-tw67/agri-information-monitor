from datetime import date

from agri_monitor.models import Article, Source
from agri_monitor.sent_state import SentArticleState


def test_state_round_trip_filters_same_url(tmp_path):
    path = tmp_path / "state" / "sent_articles.json"
    source = Source("來源", "https://source.test")
    article = Article(
        "文章標題",
        "https://EXAMPLE.test/article/?utm_source=email#top",
        date(2026, 9, 22),
    )
    state = SentArticleState()
    state.record_sent([(source, [article])], date(2026, 9, 23))
    state.save(path, date(2026, 9, 23))

    loaded = SentArticleState.load(path)
    equivalent = Article(
        "文章標題",
        "https://example.test/article",
        date(2026, 9, 22),
    )
    assert loaded.is_sent(equivalent)
    assert loaded.filter_unsent([(source, [equivalent])]) == [(source, [])]


def test_state_title_signature_matches_google_news_and_official_urls():
    source = Source("來源", "https://source.test")
    google_article = Article(
        "近期白背飛蝨密度上升 - 花蓮區農業改良場",
        "https://news.google.com/rss/articles/example",
        date(2026, 9, 22),
    )
    official_article = Article(
        "近期白背飛蝨密度上升",
        "https://www.hdares.gov.tw/theme_data.php?id=14921",
        date(2026, 9, 22),
    )
    state = SentArticleState()
    state.record_sent([(source, [google_article])], date(2026, 9, 23))
    assert state.is_sent(official_article)


def test_state_matches_official_mirror_when_dates_differ_by_one_day():
    source = Source("農業知識入口網", "https://kmweb.moa.gov.tw")
    mirror = Article(
        "近期白背飛蝨密度上升 敬請農友加強巡田並立即防治",
        "https://kmweb.moa.gov.tw/theme_data.php?id=101369",
        date(2026, 9, 23),
    )
    original = Article(
        mirror.title,
        "https://www.hdares.gov.tw/theme_data.php?id=14921",
        date(2026, 9, 22),
    )
    state = SentArticleState()
    state.record_sent([(source, [mirror])], date(2026, 10, 17))
    assert state.is_sent(original)


def test_state_prunes_records_older_than_retention_window():
    source = Source("來源", "https://source.test")
    old = Article("舊文章", "https://example.test/old", date(2025, 1, 1))
    recent = Article("新文章", "https://example.test/new", date(2026, 10, 1))
    state = SentArticleState()
    state.record_sent([(source, [old])], date(2025, 1, 2), retention_days=180)
    state.record_sent([(source, [recent])], date(2026, 10, 2), retention_days=180)
    assert not state.is_sent(old)
    assert state.is_sent(recent)
