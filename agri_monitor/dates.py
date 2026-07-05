from datetime import date, timedelta


def monitoring_window(run_date: date) -> tuple[date, date]:
    target_date = run_date - timedelta(days=1)
    return target_date, target_date


def page_title(start_date: date, end_date: date) -> str:
    if start_date == end_date:
        return f"農業資訊每日監控 (日期:{start_date.isoformat()})"
    return (
        "農業資訊監控排程任務 "
        f"(上次:{start_date.isoformat()}/ 本次:{end_date.isoformat()})"
    )
