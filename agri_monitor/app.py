import argparse
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .acri import AcriScraper, sync_acri
from .config import read_sources
from .dates import monitoring_window, page_title
from .email_report import build_report_email, parse_recipients, send_report_email
from .models import AcriSyncResult, Article, Source
from .notion import NotionClient
from .scraper import enrich_article, fetch_source, filter_and_dedupe, normalize_url

LOG = logging.getLogger(__name__)
TITLE_DATE_PREFIX = re.compile(r"^\s*\d{2,3}[-/]\d{1,2}[-/]\d{1,2}\s+")
RECOVERY_ATTEMPTS = 3
RECOVERY_DELAY_SECONDS = 20 * 60


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必要環境變數：{name}")
    return value


def _latest_reliable_article(source_name: str, articles):
    dated = [article for article in articles if article.published_date is not None]
    if not dated:
        raise RuntimeError(f"{source_name} 找不到具有可靠日期的最新版公告")
    return max(dated, key=lambda article: article.published_date)


def _matches_any(patterns: tuple[str, ...], title: str) -> bool:
    return any(re.search(pattern, title) for pattern in patterns)


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


def _article_hostname(article: Article) -> str:
    return urlsplit(article.canonical_url or article.url).netloc.lower()


def _dedupe_run_articles(
    articles: list[Article],
    seen_run_urls: set[str],
    seen_run_titles: set[tuple[date | None, str, str]],
) -> list[Article]:
    unique_articles = []
    for article in articles:
        url_key = normalize_url(article.canonical_url or article.url)
        title_key = (
            article.published_date,
            _article_hostname(article),
            _normalized_title(article.title),
        )
        if url_key in seen_run_urls or title_key in seen_run_titles:
            continue
        seen_run_urls.add(url_key)
        seen_run_titles.add(title_key)
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
]:
    """Fetch a set of sources and return successful results plus failed sources."""
    grouped: list[tuple[Source, list[Article]]] = []
    source_failures: list[tuple[str, str]] = []
    failed_sources: list[Source] = []
    seen_run_urls: set[str] = set()
    seen_run_titles: set[tuple[date | None, str, str]] = set()

    for source in sources:
        try:
            discovered = fetch_source(source)
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
            # Do not request article pages for entries whose reliable list/feed
            # date already proves they are outside this run's window.
            candidates = [
                article
                for article in discovered
                if article.published_date is None
                or start_date <= article.published_date <= end_date
            ]
            enriched = [enrich_article(article) for article in candidates]
            articles = filter_and_dedupe(enriched, start_date, end_date)
            unique_articles = _dedupe_run_articles(
                articles, seen_run_urls, seen_run_titles
            )
            grouped.append((source, unique_articles))
            LOG.info("來源完成：%s，區間內 %d 篇", source.name, len(unique_articles))
        except Exception as exc:
            source_failures.append((source.name, str(exc)))
            failed_sources.append(source)
            LOG.error("來源失敗：%s (%s)", source.name, exc)

    return grouped, source_failures, failed_sources


def _send_recovery_email(
    *,
    failed_sources: list[Source],
    start_date: date,
    end_date: date,
    report_title: str,
    username: str,
    recipients: tuple[str, ...],
    smtp_port: int,
    smtp_password: str,
) -> None:
    """Retry only initial failures, then send one consolidated recovery email."""
    pending_sources = failed_sources
    recovered: dict[Source, list[Article]] = {}
    final_failures: list[tuple[str, str]] = []

    for attempt in range(1, RECOVERY_ATTEMPTS + 1):
        LOG.warning(
            "%d 個來源待補抓；%d 分鐘後進行第 %d/%d 次補抓",
            len(pending_sources),
            RECOVERY_DELAY_SECONDS // 60,
            attempt,
            RECOVERY_ATTEMPTS,
        )
        time.sleep(RECOVERY_DELAY_SECONDS)
        grouped, final_failures, pending_sources = _collect_source_results(
            pending_sources, start_date, end_date
        )
        recovered.update(dict(grouped))

        if not pending_sources:
            notice = (
                f"補抓結果：原先失敗的 {len(failed_sources)} 個來源，"
                f"已於第 {attempt} 次補抓全部成功。"
            )
        elif attempt == RECOVERY_ATTEMPTS:
            notice = (
                f"補抓結果：已完成 {len(recovered)}/{len(failed_sources)} 個來源；"
                f"第 {attempt} 次補抓後仍有 {len(pending_sources)} 個來源失敗。"
            )
        else:
            continue

        recovered_groups = [
            (source, recovered[source])
            for source in failed_sources
            if source in recovered
        ]
        recovery_title = f"{report_title} 補抓結果（第 {attempt} 次）"
        message = build_report_email(
            recovery_title,
            recovered_groups,
            AcriSyncResult(0, 0, 0, [], []),
            final_failures,
            sender=username,
            recipients=recipients,
            include_acri_status=False,
            notice=notice,
        )
        send_report_email(
            message,
            host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com",
            port=smtp_port,
            username=username,
            password=smtp_password,
        )
        print(
            f"Recovery email sent: {recovery_title}; "
            f"recovered_sources={len(recovered)}; remaining_failures={len(pending_sources)}"
        )
        return


def run(run_date: date, dry_run: bool = False) -> int:
    sources = read_sources(os.getenv("SOURCES_FILE", "sources.yml"))
    LOG.info("從來源設定檔讀取到 %d 個啟用來源", len(sources))

    start_date, end_date = monitoring_window(run_date)
    title = page_title(start_date, end_date)
    token = _required("NOTION_TOKEN")
    grouped, source_failures, failed_sources = _collect_source_results(
        sources, start_date, end_date
    )
    failures = len(source_failures)

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
            f"acri_new={acri_result.planned_count}"
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
        sender=username,
        recipients=recipients,
    )
    smtp_password = _required("SMTP_APP_PASSWORD")
    send_report_email(
        message,
        host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com",
        port=smtp_port,
        username=username,
        password=smtp_password,
    )
    print(
        f"Email sent: {title}; articles={article_count}; "
        f"acri_created={len(acri_result.created)}"
    )
    if failed_sources:
        _send_recovery_email(
            failed_sources=failed_sources,
            start_date=start_date,
            end_date=end_date,
            report_title=title,
            username=username,
            recipients=recipients,
            smtp_port=smtp_port,
            smtp_password=smtp_password,
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
        timezone = ZoneInfo(os.getenv("TIMEZONE", "Asia/Taipei"))
        effective_date = args.run_date or datetime.now(timezone).date()
        return run(effective_date, args.dry_run)
    except Exception as exc:
        LOG.exception("任務失敗：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
