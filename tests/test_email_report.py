from email.message import EmailMessage

from agri_monitor import email_report


def test_parse_recipients_accepts_comma_and_semicolon_lists():
    assert email_report.parse_recipients(
        "first@example.com; second@example.com, third@example.com"
    ) == (
        "first@example.com",
        "second@example.com",
        "third@example.com",
    )


def test_send_report_email_uses_starttls_and_app_password(monkeypatch):
    calls = []

    class FakeSmtp:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            calls.append(("close",))

        def ehlo(self):
            calls.append(("ehlo",))

        def starttls(self, *, context):
            calls.append(("starttls", context is not None))

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(("send", message["To"]))

    monkeypatch.setattr(email_report.smtplib, "SMTP", FakeSmtp)
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "reader@example.com"
    message["Subject"] = "test"
    message.set_content("body")

    email_report.send_report_email(
        message,
        host="smtp.gmail.com",
        port=587,
        username="sender@example.com",
        password="app-password",
    )

    assert calls[0] == ("connect", "smtp.gmail.com", 587, 30)
    assert calls.count(("ehlo",)) == 2
    assert ("starttls", True) in calls
    assert ("login", "sender@example.com", "app-password") in calls
    assert ("send", "reader@example.com") in calls
    assert calls[-1] == ("close",)
