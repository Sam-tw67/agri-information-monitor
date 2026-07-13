from datetime import date, timedelta


def monitoring_window(run_date: date) -> tuple[date, date]:
    end_date = run_date - timedelta(days=1)
    if run_date.weekday() == 0:
        return run_date - timedelta(days=2), end_date
    return end_date, end_date


def page_title(start_date: date, end_date: date) -> str:
    if start_date == end_date:
        return f"農業資訊每日監控 (日期:{start_date.isoformat()})"
    return f"農業資訊每日監控 (日期:{start_date.isoformat()}~{end_date.isoformat()})"
