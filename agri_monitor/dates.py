from datetime import date, timedelta


def monitoring_window(run_date: date) -> tuple[date, date]:
    return run_date - timedelta(days=7), run_date - timedelta(days=1)


def page_title(start_date: date, end_date: date) -> str:
    return (
        "農業資訊監控排程任務 "
        f"(上次:{start_date.isoformat()}/ 本次:{end_date.isoformat()})"
    )
