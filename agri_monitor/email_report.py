import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import getaddresses

from .models import AcriSyncResult, Article, Source

LOG = logging.getLogger(__name__)


def parse_recipients(value: str) -> tuple[str, ...]:
    recipients = tuple(
        address
        for _, address in getaddresses([value.replace(";", ",")])
        if address
    )
    if not recipients:
        raise RuntimeError("EMAIL_TO 未包含有效的收件人地址")
    return recipients


def _no_update_label(source_name: str) -> str:
    label = source_name.split("－", 1)[0].strip()
    return label.replace("農改場", "改良場")


def _dedupe_labels(labels: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for label in labels:
        if label and label not in seen:
            seen.add(label)
            result.append(label)
    return result


def _report_content(
    grouped_articles: list[tuple[Source, list[Article]]],
    acri_result: AcriSyncResult,
    source_failures: list[tuple[str, str]],
    *,
    include_acri_status: bool,
    notice: str | None,
) -> tuple[list[str], list[str]]:
    plain: list[str] = []
    html_parts: list[str] = []
    no_update_labels: list[str] = []

    for source, articles in grouped_articles:
        if not articles:
            if source.show_no_update:
                no_update_labels.append(_no_update_label(source.name))
            continue
        plain.append(f"[{source.name}]")
        for article in articles:
            plain.extend([f"- {article.title}", f"  {article.url}"])
        plain.append("")
        html_parts.append(f"<h2>{html.escape(source.name)}</h2><ul>")
        html_parts.extend(
            '<li><a href="%s">%s</a></li>'
            % (html.escape(article.url, quote=True), html.escape(article.title))
            for article in articles
        )
        html_parts.append("</ul>")

    if include_acri_status and acri_result.created:
        plain.append("[ACRI 農藥問答集]")
        html_parts.append("<h2>ACRI 農藥問答集</h2><ul>")
        for created in acri_result.created:
            plain.extend([f"- {created.entry.question}", f"  {created.notion_url}"])
            html_parts.append(
                '<li><a href="%s">%s</a></li>'
                % (
                    html.escape(created.notion_url, quote=True),
                    html.escape(created.entry.question),
                )
            )
        plain.append("")
        html_parts.append("</ul>")
    elif include_acri_status and acri_result.error is None:
        no_update_labels.append("ACRI 農藥問答集")

    no_update_labels = _dedupe_labels(no_update_labels)
    if no_update_labels:
        message = f"以下監控項目無新增項目來源：{'、'.join(no_update_labels)}。"
        plain.extend([message, ""])
        html_parts.append(f"<p>{html.escape(message)}</p>")

    if source_failures:
        plain.append("[抓取失敗]")
        html_parts.append("<h2>抓取失敗</h2><ul>")
        for source_name, error in source_failures:
            plain.append(f"- {source_name}：{error}")
            html_parts.append(
                f"<li>{html.escape(source_name)}：{html.escape(error)}</li>"
            )
        plain.append("")
        html_parts.append("</ul>")

    if include_acri_status and acri_result.error:
        message = f"ACRI 同步失敗：{acri_result.error}"
        plain.extend([message, ""])
        html_parts.append(f"<p><strong>{html.escape(message)}</strong></p>")

    if include_acri_status and acri_result.duplicate_numbers:
        numbers = "、".join(acri_result.duplicate_numbers)
        message = f"ACRI 資料庫已有重複編號（本次未重複新增）：{numbers}"
        plain.extend([message, ""])
        html_parts.append(f"<p>{html.escape(message)}</p>")

    if notice:
        plain[0:0] = [notice, ""]
        html_parts.insert(0, f"<p><strong>{html.escape(notice)}</strong></p>")

    return plain, html_parts


def build_report_email(
    report_title: str,
    grouped_articles: list[tuple[Source, list[Article]]],
    acri_result: AcriSyncResult,
    source_failures: list[tuple[str, str]],
    *,
    sender: str,
    recipients: tuple[str, ...],
    include_acri_status: bool = True,
    notice: str | None = None,
) -> EmailMessage:
    article_count = sum(len(articles) for _, articles in grouped_articles)
    acri_count = len(acri_result.created)
    plain_content, html_content = _report_content(
        grouped_articles,
        acri_result,
        source_failures,
        include_acri_status=include_acri_status,
        notice=notice,
    )

    message = EmailMessage()
    subject_parts = [report_title, f"一般 {article_count} 篇"]
    if include_acri_status:
        subject_parts.append(f"ACRI {acri_count} 筆")
    message["Subject"] = "｜".join(subject_parts)
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    plain_summary = [report_title, f"一般文章：{article_count} 篇"]
    if include_acri_status:
        plain_summary.append(f"ACRI 新增：{acri_count} 筆")
    message.set_content("\n".join([*plain_summary, "", *plain_content]).rstrip() + "\n")
    html_summary = f"<p>一般文章：{article_count} 篇"
    if include_acri_status:
        html_summary += f"<br>ACRI 新增：{acri_count} 筆"
    html_summary += "</p>"
    message.add_alternative(
        """<!doctype html>
<html lang="zh-Hant"><body>
<h1>%s</h1>
%s
%s
</body></html>"""
        % (
            html.escape(report_title),
            html_summary,
            "\n".join(html_content),
        ),
        subtype="html",
    )
    return message


def send_report_email(
    message: EmailMessage,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
) -> None:
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(username, password)
        smtp.send_message(message)
    LOG.info("Email 已寄送至 %s", message["To"])
