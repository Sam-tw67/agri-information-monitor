import argparse
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .acri import AcriScraper, sync_acri
from .config import read_sources
from .dates import monitoring_window, page_title
from .email_report import build_report_email, parse_recipients, send_report_email
from .models import AcriSyncResult, Article, Source
from .notion import NotionClient
from .scraper import enrich_article, fetch_source, filter_and_dedupe, normalize_url
from .sent_state import SentArticleState

LOG = logging.getLogger(__name__)
TITLE_DATE_PREFIX = re.compile(r"^\s*\d{2,3}[-/]\d{1,2}[-/]\d{1,2}\s+")
SOURCE_RETRY_DELAY_SECONDS = 5 * 60
SYSTEMIC_NETWORK_RETRY_ATTEMPTS = 3
SYSTEMIC_NETWORK_RETRY_DELAY_SECONDS = 10 * 60
SOURCE_FETCH_WORKERS = 6
DEFAULT_ARTICLE_LOOKBACK_DAYS = 30
NETWORK_FAILURE_MARKERS = (
    "Network is unreachable",
    "Failed to establish a new connection",
    "timed out",
    "Temporary failure in name resolution",
)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必要環境變數：{name}")
    return value


def _positive_int_setting(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必須是整數") from exc
    if value < 1:
        raise RuntimeError(f"{name} 必須大於 0")
    return value


def _latest_reliable_article(source_name: str, articles):
    dated = [article for article in articles if article.published_date is not None]
    if not dated:
        raise RuntimeError(f"{source_name} 找不到具有可靠日期的最新版公告")
    return max(dated, key=lambda article: article.published_date)


def _matches_any(patterns: tuple[str, ...], title: str) -> bool:
    return any(re.search(pattern, title) for pattern in patterns)


def _all_sources_have_network_failures(
    source_failures: list[tuple[str, str]], source_count: int
) -> bool:
    return source_count > 0 and len(source_failures) == source_count and all(
        any(marker in error for marker in NETWORK_FAILURE_MARKERS)
        for _, error in source_failures
    )


def _apply_source_title_filters(source: Source, articles: list[Article]) -> list[Article]:
    result = articles
    if source.include_title_patterns:
        result = [
            article
            for article in result
            if _matches_any(source.include_title_patterns, article.title)
        ]
    if source.exclude_title_patterns:
        result = [
            article
            for article in result
            if not _matches_any(source.exclude_title_patterns, article.title)
        ]
    return result


def _normalized_title(title: str) -> str:
    title = TITLE_DATE_PREFIX.sub("", title)
    return re.sub(r"\s+", " ", title).strip()


def _dedupe_run_articles(
    articles: list[Article],
    seen_run_urls: set[str],
    seen_run_titles: set[tuple[date | None, str]],
) -> list[Article]:
    unique_articles = []
    for article in articles:
        url_key = normalize_url(article.canonical_url or article.url)
        normalized_title = _normalized_title(article.title)
        published = article.published_date
        nearby_title_keys = {(published, normalized_title)}
        if published is not None:
            nearby_title_keys.update(
                {
                    (published - timedelta(days=1), normalized_title),
                    (published + timedelta(days=1), normalized_title),
                }
            )
        if url_key in seen_run_urls or nearby_title_keys & seen_run_titles:
            continue
        seen_run_urls.add(url_key)
        seen_run_titles.add((published, normalized_title))
        unique_articles.append(article)
    return unique_articles


def _collect_source_results(
    sources: list[Source],
    start_date: date,
    end_date: date,
) -> tuple[
    list[tuple[Source, list[Article]]],
    list[tuple[str, str]],
    list[Source],
    list[tuple[str, str]],
]:
    """Fetch a set of sources and return successful results plus failed sources."""
    grouped: list[tuple[Source, list[Article]]] = []
    source_failures: list[tuple[str, str]] = []
    failed_sources: list[Source] = []
    source_warnings: list[tuple[str, str]] = []
    seen_run_urls: set[str] = set()
    seen_run_titles: set[tuple[date | None, str]] = set()

    worker_count = min(SOURCE_FETCH_WORKERS, len(sources))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        fetches = {source: executor.submit(fetch_source, source) for source in sources}

    for source in sources:
        try:
            discovered = fetches[source].result()
            degraded_warning = getattr(discovered, "degraded_warning", None)
            if source.include_title_patterns:
                before_count = len(discovered)
                discovered = _apply_source_title_filters(
                    Source(
                        source.name,
                        source.url,
                        include_title_patterns=source.include_title_patterns,
                    ),
                    discovered,
                )
                LOG.info(
                    "來源標題白名單：%s，保留 %d/%d 篇候選文章",
                    source.name,
                    len(discovered),
                    before_count,
                )
            if source.exclude_title_patterns:
                before_count = len(discovered)
                discovered = _apply_source_title_filters(
                    Source(
                        source.name,
                        source.url,
                        exclude_title_patterns=source.exclude_title_patterns,
                    ),
                    discovered,
                )
                LOG.info(
                    "來源標題排除規則：%s，保留 %d/%d 篇候選文章",
                    source.name,
                    len(discovered),
                    before_count,
                )
            if source.latest_only:
                discovered = [_latest_reliable_article(source.name, discovered)]
                LOG.info(
                    "來源僅保留最新版：%s（%s）",
                    discovered[0].title,
                    discovered[0].published_date,
                )
            # Only entries without a reliable list/feed date need an article-page
            # request to recover their publication date.
            candidates = [
                article
                for article in discovered
                if article.published_date is None
                or start_date <= article.published_date <= end_date
            ]
            enriched = [
                article
                if article.published_date is not None
                else enrich_article(article)
                for article in candidates
            ]
            articles = filter_and_dedupe(enriched, start_date, end_date)
            unique_articles = _dedupe_run_articles(
                articles, seen_run_urls, seen_run_titles
            )
            grouped.append((source, unique_articles))
            if degraded_warning:
                source_warnings.append((source.name, degraded_warning))
            LOG.info("來源完成：%s，區間內 %d 篇", source.name, len(unique_articles))
        except Exception as exc:
            source_failures.append((source.name, str(exc)))
            failed_sources.append(source)
            LOG.error("來源失敗：%s (%s)", source.name, exc)

    return grouped, source_failures, failed_sources, source_warnings


def run(run_date: date, dry_run: bool = False) -> int:
    sources = read_sources(os.getenv("SOURCES_FILE", "sources.yml"))
    LOG.info("從來源設定檔讀取到 %d 個啟用來源", len(sources))

    report_start_date, end_date = monitoring_window(run_date)
    lookback_days = _positive_int_setting(
        "ARTICLE_LOOKBACK_DAYS", DEFAULT_ARTICLE_LOOKBACK_DAYS
    )
    start_date = min(
        report_start_date,
        end_date - timedelta(days=lookback_days - 1),
    )
    state_path = Path(
        os.getenv("ARTICLE_STATE_FILE", ".state/sent_articles.json").strip()
        or ".state/sent_articles.json"
    )
    sent_state = SentArticleState.load(state_path)
    title = page_title(report_start_date, end_date)
    LOG.info(
        "一般來源回溯區間：%s 至 %s；已寄送紀錄 %d 筆",
        start_date,
        end_date,
        len(sent_state.records),
    )
    token = _required("NOTION_TOKEN")
    grouped, source_failures, failed_sources, source_warnings = _collect_source_results(
        sources, start_date, end_date
    )
    if _all_sources_have_network_failures(source_failures, len(sources)):
        for attempt in range(1, SYSTEMIC_NETWORK_RETRY_ATTEMPTS + 1):
            LOG.warning(
                "所有來源的網路連線都失敗；%d 分鐘後進行第 %d/%d 次全域重試",
                SYSTEMIC_NETWORK_RETRY_DELAY_SECONDS // 60,
                attempt,
                SYSTEMIC_NETWORK_RETRY_ATTEMPTS,
            )
            time.sleep(SYSTEMIC_NETWORK_RETRY_DELAY_SECONDS)
            (
                grouped,
                source_failures,
                failed_sources,
                source_warnings,
            ) = _collect_source_results(sources, start_date, end_date)
            if not _all_sources_have_network_failures(source_failures, len(sources)):
                break
    if failed_sources and not _all_sources_have_network_failures(
        source_failures, len(sources)
    ):
        LOG.warning(
            "%d 個來源待補抓；%d 分鐘後重試一次，再寄送結果",
            len(failed_sources),
            SOURCE_RETRY_DELAY_SECONDS // 60,
        )
        time.sleep(SOURCE_RETRY_DELAY_SECONDS)
        (
            retried_grouped,
            source_failures,
            failed_sources,
            retried_warnings,
        ) = _collect_source_results(failed_sources, start_date, end_date)
        source_warnings.extend(retried_warnings)
        articles_by_source = dict(grouped)
        articles_by_source.update(dict(retried_grouped))
        grouped = [
            (source, articles_by_source[source])
            for source in sources
            if source in articles_by_source
        ]
    failures = len(source_failures)
    grouped = sent_state.filter_unsent(grouped)
    recovered_count = sum(
        1
        for _, articles in grouped
        for article in articles
        if article.published_date is not None
        and article.published_date < report_start_date
    )

    try:
        acri_client = NotionClient(
            token,
            _required("ACRI_NOTION_DATABASE_ID"),
            os.getenv("ACRI_NOTION_DATA_SOURCE_ID", "").strip(),
        )
        acri_result = sync_acri(
            AcriScraper(_required("ACRI_SOURCE_URL")),
            acri_client,
            dry_run=dry_run,
        )
    except Exception as exc:
        LOG.exception("ACRI 同步失敗：%s", exc)
        acri_result = AcriSyncResult(
            discovered_count=0,
            existing_count=0,
            planned_count=0,
            created=[],
            duplicate_numbers=[],
            error=str(exc),
        )

    article_count = sum(len(articles) for _, articles in grouped)
    if dry_run:
        LOG.info(
            "DRY-RUN：預計報告：%s；一般文章數：%d；ACRI 待新增：%d；不寄信",
            title,
            article_count,
            acri_result.planned_count,
        )
        print(
            f"DRY-RUN report={title} articles={article_count} "
            f"recovered={recovered_count} acri_new={acri_result.planned_count}"
        )
        return 1 if acri_result.error else 0

    username = _required("SMTP_USERNAME")
    recipients = parse_recipients(_required("EMAIL_TO"))
    try:
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT 必須是整數") from exc
    message = build_report_email(
        title,
        grouped,
        acri_result,
        source_failures,
        source_warnings=source_warnings,
        recovered_count=recovered_count,
        sender=username,
        recipients=recipients,
    )
    send_report_email(
        message,
        host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com",
        port=smtp_port,
        username=username,
        password=_required("SMTP_APP_PASSWORD"),
    )
    if article_count:
        sent_state.record_sent(grouped, run_date)
        sent_state.save(state_path, run_date)
        LOG.info("已更新一般文章寄送紀錄：%s", state_path)
    print(
        f"Email sent: {title}; articles={article_count}; "
        f"recovered={recovered_count}; acri_created={len(acri_result.created)}"
    )
    if failures == len(sources) and acri_result.error:
        raise RuntimeError("全部一般來源與 ACRI 皆失敗；錯誤報告已寄出")
    if acri_result.error:
        raise RuntimeError(
            "ACRI 同步失敗；專用 Notion database 已保留成功項目，"
            "錯誤報告已寄出，工作流程標記失敗"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="每日農業資訊監控")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不寫入 Notion，也不寄送 Email",
    )
    parser.add_argument("--run-date", type=date.fromisoformat, help="執行日期 YYYY-MM-DD；預設為設定時區的今天")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.run_date is not None:
            effective_date = args.run_date
        else:
            timezone = ZoneInfo(os.getenv("TIMEZONE", "Asia/Taipei"))
            effective_date = datetime.now(timezone).date()
        return run(effective_date, args.dry_run)
    except Exception as exc:
        LOG.exception("任務失敗：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
