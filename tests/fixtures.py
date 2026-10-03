"""Payloads shaped like the Google Calendar and Google Tasks APIs return them.

Every name, address and ID here is invented. Times are for a board in
America/Los_Angeles on Friday 2 October 2026 (UTC-7).
"""

LA = "-07:00"


def timed(event_id, summary, start, end, **extra):
    """A timed event; *start* and *end* are local ``HH:MM`` on 2026-10-02 unless they hold a date."""

    def when(value):
        if "T" in value:
            return value
        return f"2026-10-02T{value}:00{LA}"

    event = {
        "kind": "calendar#event",
        "id": event_id,
        "iCalUID": f"{event_id}@google.com",
        "status": "confirmed",
        "summary": summary,
        "eventType": "default",
        "start": {"dateTime": when(start), "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": when(end), "timeZone": "America/Los_Angeles"},
    }
    event.update(extra)
    return event


def all_day(event_id, summary, start_date, end_date, **extra):
    event = {
        "kind": "calendar#event",
        "id": event_id,
        "iCalUID": f"{event_id}@google.com",
        "status": "confirmed",
        "summary": summary,
        "eventType": "default",
        "start": {"date": start_date},
        "end": {"date": end_date},
    }
    event.update(extra)
    return event


def primary_events():
    return [
        all_day("evt_fundraiser", "Fall Fundraiser", "2026-10-02", "2026-10-03"),
        timed("evt_standup", "Standup", "09:00", "09:15"),
        timed("evt_review", "Design Review", "09:45", "10:30", location="Room 4B"),
        timed("evt_lunch", "Lunch with Sam", "13:00", "14:00", location="Cafe Rio, 12 Main St"),
        timed(
            "evt_budget",
            "Budget Sync",
            "15:00",
            "15:30",
            attendees=[
                {"email": "test_user@example.com", "self": True, "responseStatus": "declined"},
                {"email": "test_other@example.com", "responseStatus": "accepted"},
            ],
        ),
        timed("evt_cancelled", "Old Meeting", "15:30", "16:00", status="cancelled"),
        timed("evt_dentist", "Dentist", "16:00", "17:00"),
        all_day(
            "evt_wfh",
            "Home",
            "2026-10-02",
            "2026-10-03",
            eventType="workingLocation",
        ),
        timed("evt_gym", "Gym", "2026-10-03T08:00:00-07:00", "2026-10-03T09:00:00-07:00"),
    ]


def events_response(items):
    return {
        "kind": "calendar#events",
        "summary": "test_user@example.com",
        "timeZone": "America/Los_Angeles",
        "items": items,
    }


def task_lists():
    return {
        "kind": "tasks#taskLists",
        "items": [
            {"kind": "tasks#taskList", "id": "list_mine", "title": "My Tasks"},
            {"kind": "tasks#taskList", "id": "list_home", "title": "Home"},
        ],
    }


def task(task_id, title, due=None, status="needsAction", position="00000000000000000001"):
    item = {"kind": "tasks#task", "id": task_id, "title": title, "status": status, "position": position}
    if due:
        item["due"] = f"{due}T00:00:00.000Z"
    return item


def tasks_by_list():
    return {
        "list_mine": [
            task("t_plants", "Water the plants", "2026-10-02", position="00000000000000000002"),
            task("t_expenses", "File expenses", "2026-10-01"),
            task("t_done", "Book flights", "2026-10-02", status="completed"),
            task("t_later", "Renew passport", "2026-10-20"),
            task("t_nodue", "Someday idea"),
        ],
        "list_home": [
            task("t_trash", "Take out trash", "2026-10-02", position="00000000000000000001"),
        ],
    }


def google_error(code, reason, message="Request failed", detail_reason=None, status="PERMISSION_DENIED"):
    body = {
        "error": {
            "code": code,
            "message": message,
            "errors": [{"reason": reason, "message": message}],
            "status": status,
        }
    }
    if detail_reason:
        body["error"]["details"] = [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": detail_reason, "domain": "googleapis.com"}
        ]
    return body
