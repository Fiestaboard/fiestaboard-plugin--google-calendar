"""Shared fixtures for the Google Calendar & Tasks plugin tests.

``google_api`` patches ``requests.get`` with a fake Google Calendar and Tasks
API answering the endpoints this plugin uses (see ``fixtures.py``). Tests
change its data or queue raw responses to simulate errors. No test reaches
the network.

``get_oauth_token`` is patched on the plugin instance: the platform's OAuth
service is not under test here, only how the plugin uses its answer.
"""

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import pytest

from plugins.google_calendar import GoogleCalendarPlugin

from .fixtures import events_response, primary_events, task_lists, tasks_by_list

MANIFEST_PATH = Path(__file__).parent.parent / "manifest.json"
CAL_API = "https://www.googleapis.com/calendar/v3"
TASKS_API = "https://tasks.googleapis.com/tasks/v1"
TEST_TOKEN = "test_access_token"
TEST_CLIENT_ID = "test_client.apps.googleusercontent.com"
# Friday 2 October 2026, 10:00 in Los Angeles.
NOW = datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, body=None, status_code=200, headers=None):
        self._body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.content = b"" if body is None else json.dumps(body).encode()

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeGoogle:
    """Answers requests.get the way Google's Calendar and Tasks APIs would."""

    def __init__(self):
        self.calendars = {"primary": primary_events()}
        self.task_lists = task_lists()
        self.tasks = tasks_by_list()
        # Raw responses (or exceptions) for the next calls whose URL contains a key.
        self.overrides = {}
        self.calls = []

    def fail(self, url_part, *responses):
        self.overrides.setdefault(url_part, deque()).extend(responses)

    def urls(self):
        return [url for url, _ in self.calls]

    def __call__(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append((url, {"params": params, "headers": headers, "timeout": timeout, **kwargs}))
        for part, queue in self.overrides.items():
            if part in url and queue:
                answer = queue.popleft()
                if isinstance(answer, Exception):
                    raise answer
                return answer
        params = params or {}
        if url.startswith(f"{CAL_API}/calendars/") and url.endswith("/events"):
            calendar_id = unquote(url[len(f"{CAL_API}/calendars/") : -len("/events")])
            if calendar_id not in self.calendars:
                return FakeResponse({"error": {"code": 404, "message": "Not Found"}}, 404)
            return FakeResponse(events_response(self.calendars[calendar_id]))
        if url == f"{TASKS_API}/users/@me/lists":
            return FakeResponse(self.task_lists)
        if url.startswith(f"{TASKS_API}/lists/") and url.endswith("/tasks"):
            list_id = url[len(f"{TASKS_API}/lists/") : -len("/tasks")]
            items = []
            for item in self.tasks.get(list_id, []):
                if params.get("showCompleted") == "false" and item["status"] == "completed":
                    continue
                due = item.get("due")
                if ("dueMin" in params or "dueMax" in params) and not due:
                    continue
                if "dueMin" in params and due < params["dueMin"]:
                    continue
                if "dueMax" in params and due >= params["dueMax"]:
                    continue
                items.append(item)
            return FakeResponse({"kind": "tasks#tasks", "items": items})
        return FakeResponse({"error": {"code": 404, "message": "Not Found"}}, 404)


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.now = NOW

    def advance(self, seconds):
        from datetime import timedelta

        self.mono += seconds
        self.now = self.now + timedelta(seconds=seconds)


class Token:
    value = TEST_TOKEN


@pytest.fixture
def manifest_data():
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture
def google_api():
    fake = FakeGoogle()
    with patch("plugins.google_calendar.requests.get", side_effect=fake):
        yield fake


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr("plugins.google_calendar._monotonic", lambda: c.mono)
    monkeypatch.setattr("plugins.google_calendar._utcnow", lambda: c.now)
    return c


@pytest.fixture
def token():
    return Token()


@pytest.fixture
def config():
    return {
        "enabled": True,
        "client_id": TEST_CLIENT_ID,
        "client_secret": "test_client_secret",
        "timezone": "America/Los_Angeles",
    }


@pytest.fixture
def plugin(manifest_data, config, clock, token):
    p = GoogleCalendarPlugin(manifest_data)
    p.config = dict(config)
    p.get_oauth_token = lambda: token.value
    return p
