"""Activity log (app/activity.py): one line per newly active user and per kind of action a minute, never per request."""
import logging

from app import activity


def test_logs_new_users_and_actions_without_repeating(caplog, monkeypatch):
    monkeypatch.setattr(activity, "_seen", {})
    monkeypatch.setattr(activity, "_actions", {})
    with caplog.at_level(logging.WARNING, logger="talkback.activity"):
        activity.note("a@example.com", "GET", "/api/books", now=0)
        activity.note("a@example.com", "GET", "/api/books", now=5)                       # same user, a read: silent
        activity.note("a@example.com", "POST", "/api/books/upload", now=6)
        activity.note("a@example.com", "POST", "/api/books/upload", now=20)              # within a minute: silent
        activity.note("a@example.com", "POST", "/api/books/upload", now=70)              # a minute later: logged
        activity.note("ip:1.2.3.4", "POST", "/api/public/process", now=71)
        activity.note("a@example.com", "GET", "/api/books", now=70 + activity.IDLE_S + 1)  # back after idle
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [
        "user active: a@example.com",
        "user action: a@example.com POST /api/books/upload",
        "user action: a@example.com POST /api/books/upload",
        "user active: ip:1.2.3.4",
        "user action: ip:1.2.3.4 POST /api/public/process",
        "user active: a@example.com",
    ]


def test_never_raises(monkeypatch):
    monkeypatch.setattr(activity, "_lock", None)   # broken state: still no exception
    activity.note("x", "GET", "/")
