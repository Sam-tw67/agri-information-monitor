import argparse
import logging
import os
import re
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .acri import AcriScraper, sync_acri
from .config import read_sources
from .dates import monitoring_window, page_title
from .models import AcriSyncResult
from .notion import NotionClient, build_blocks
from .scraper import enrich_article, fetch_source, filter_and_dedupe

LOG = logging.getLogger(__name__)


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


def run(run_date: date, dry_run: bool = False) -> int:
    sources = read_sources(os.getenv("SOURCES_FILE", "sources.yml"))
    LOG.info("從來源設定檔讀取到 %d 個啟用來源", len(sources))

    start_date, end_date = monitoring_window(run_date)
    title = page_title(start_date, end_date)
    token = _required("NOTION_TOKEN")
    client = NotionClient(
        token,
        _required("NOTION_DATABASE_ID"),
        os.getenv("NOTION_DATA_SOURCE_ID", "").strip(),
    )
    client.validate_target()
    LOG.info("週報 Notion 身分、data source 權限與必要欄位驗證完成")
    grouped = []
    failures = 0
    seen_run_urls: set[str] = set()
    for source in sources:
        try:
            discovered = fetch_source(source)
            if source.include_title_patterns:
                before_count = len(discovered)
                discovered = [
                    article
                    for article in discovered
                    if any(
                        re.search(pattern, article.title)
                        for pattern in source.include_title_patterns
                    )
                ]
                LOG.info(
                    "來源標題白名單：%s，保留 %d/%d 篇候選文章",
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
                article for article in discovered
                if article.published_date is None or start_date <= article.published_date <= end_date
            ]
            enriched = [enrich_article(article) for article in candidates]
            articles = filter_and_dedupe(enriched, start_date, end_date)
            unique_articles = []
            from .scraper import normalize_url
            for article in articles:
                key = normalize_url(article.canonical_url or article.url)
                if key not in seen_run_urls:
                    seen_run_urls.add(key)
                    unique_articles.append(article)
            grouped.append((source, unique_articles))
            LOG.info("來源完成：%s，區間內 %d 篇", source.name, len(unique_articles))
        except Exception as exc:
            failures += 1
            LOG.error("來源失敗：%s (%s)", source.name, exc)

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

    if failures == len(sources) and acri_result.error:
        raise RuntimeError("全部一般來源與 ACRI 皆失敗；不建立 Notion page")

    article_count = sum(len(articles) for _, articles in grouped)
    if dry_run:
        LOG.info(
            "DRY-RUN：預計建立或更新 page：%s；一般文章數：%d；ACRI 待新增：%d",
            title,
            article_count,
            acri_result.planned_count,
        )
        print(
            f"DRY-RUN page={title} articles={article_count} "
            f"acri_new={acri_result.planned_count}"
        )
        return 1 if acri_result.error else 0

    action = client.upsert(title, build_blocks(grouped, acri_result))
    print(
        f"Notion page {action}: {title}; articles={article_count}; "
        f"acri_created={len(acri_result.created)}"
    )
    if acri_result.error:
        raise RuntimeError(
            "ACRI 同步失敗；週報已保留成功項目與錯誤內容，工作流程標記失敗"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="每週農業資訊監控")
    parser.add_argument("--dry-run", action="store_true", help="不寫入 Notion")
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
