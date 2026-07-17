import requests
import pytest

from agri_monitor import scraper


def _response(status_code: int, url: str = "https://source.test/") -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.url = url
    return response


def test_get_retries_connection_errors_with_delays(monkeypatch):
    outcomes = [
        requests.ConnectionError("network unreachable"),
        requests.Timeout("timed out"),
        _response(200),
    ]
    calls = []
    sleeps = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(scraper.requests, "get", fake_get)
    monkeypatch.setattr(scraper.time, "sleep", sleeps.append)

    assert scraper._get("https://source.test/").status_code == 200
    assert len(calls) == 3
    assert sleeps == [3, 10]


def test_get_retries_three_times_then_raises(monkeypatch):
    calls = []
    sleeps = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        raise requests.ConnectionError("network unreachable")

    monkeypatch.setattr(scraper.requests, "get", fake_get)
    monkeypatch.setattr(scraper.time, "sleep", sleeps.append)

    with pytest.raises(requests.ConnectionError, match="network unreachable"):
        scraper._get("https://source.test/")

    assert len(calls) == 4
    assert sleeps == [3, 10, 30]


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
def test_get_retries_retryable_http_statuses(monkeypatch, status_code):
    responses = [_response(status_code), _response(200)]
    sleeps = []

    monkeypatch.setattr(scraper.requests, "get", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(scraper.time, "sleep", sleeps.append)

    assert scraper._get("https://source.test/").status_code == 200
    assert sleeps == [3]


def test_get_does_not_retry_other_client_errors(monkeypatch):
    calls = []
    sleeps = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return _response(404)

    monkeypatch.setattr(scraper.requests, "get", fake_get)
    monkeypatch.setattr(scraper.time, "sleep", sleeps.append)

    with pytest.raises(requests.HTTPError):
        scraper._get("https://source.test/")

    assert len(calls) == 1
    assert sleeps == []
