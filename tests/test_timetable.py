from datetime import date, datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest

from infomentor_mcp.errors import (
    AuthenticationExpiredError,
    InfoMentorError,
    UnexpectedInfoMentorResponse,
)
from infomentor_mcp.features.timetable import Timetable
from infomentor_mcp.models import TimetableLesson

PATH = "/timetable/timetable/gettimetablelist"


def lesson(day="2026-10-23", **overrides):
    return {
        "title": "Synthetic subject", "start": day + "T10:00:00",
        "end": day + "T11:00:00", "allDay": False, "details": "Synthetic group",
        "notes": {"roomInfo": "Room A", "tutors": "Teacher A, Teacher B",
                  "timetableNotes": "Synthetic change"}, **overrides,
    }


@pytest.mark.parametrize("child_ids", [None, ["first", "first"], []])
def test_timetable_form_dates_dst_details_and_separate_children(fake_hub, hub, child_ids):
    # Identical entries must remain separate, even for the same pupil.
    fake_hub.route(PATH, lambda request, child: [lesson("2026-10-26"), lesson(), lesson()])
    targets = ["first", "second"] if child_ids is None else list(dict.fromkeys(child_ids))
    items = Timetable(hub).lessons(date(2026, 10, 23), date(2026, 10, 26), child_ids)
    assert len(items) == 3 * len(targets)
    assert [(i.starts_at.date().day, i.child_id) for i in items] == [
        (day, child) for day in (23, 26) for child in targets
        for _ in range(2 if day == 23 else 1)
    ]
    if targets:
        assert items[0].starts_at.isoformat().endswith("+02:00")
        assert items[-1].starts_at.isoformat().endswith("+01:00")
        assert items[0].room == "Room A" and items[0].teachers == "Teacher A, Teacher B"
        assert items[0].notes == "Synthetic change" and items[0].details == "Synthetic group"
    # One request per child, even across DST.
    assert fake_hub.read_by(PATH) == targets
    assert fake_hub.selected == "second"
    offset = datetime.now(ZoneInfo("Europe/Stockholm")).utcoffset()
    for request in fake_hub.requests(PATH):
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        assert parse_qs(request.content.decode()) == {
            "start": ["2026-10-23"], "end": ["2026-10-27"],
            "UTCOffset": [str(-int(offset.total_seconds() // 60))],
        }


@pytest.mark.parametrize("payload", [
    {}, ["private-invalid"], [lesson(start="private-invalid")],
    [lesson(end="2026-10-23T09:00:00")], [lesson(notes=[])],
    [lesson(notes={"tutors": ["private-invalid"]})], [lesson("2026-10-24")],
])
def test_invalid_timetable_fails_closed_and_restores(fake_hub, hub, payload):
    fake_hub.route(PATH, lambda request, child: payload)
    with pytest.raises(UnexpectedInfoMentorResponse) as error:
        Timetable(hub).lessons(date(2026, 10, 23), date(2026, 10, 23))
    assert "private-invalid" not in str(error.value)
    assert fake_hub.read_by(PATH) == ["first"]
    assert fake_hub.selected == "second"


@pytest.mark.parametrize("status", [200, 401, 403])
def test_empty_schedule_is_not_an_auth_failure_and_form_uses_auth_guards(
    fake_hub, hub, status,
):
    fake_hub.route(PATH, lambda request, child: httpx.Response(status, json=[]))
    if status == 200:
        assert Timetable(hub).lessons(date(2026, 10, 23), date(2026, 10, 23)) == []
    elif status == 401:
        # Without session renewal, an expired session requires login.
        with pytest.raises(AuthenticationExpiredError):
            Timetable(hub).lessons(date(2026, 10, 23), date(2026, 10, 23))
    else:
        with pytest.raises(InfoMentorError, match="403"):
            Timetable(hub).lessons(date(2026, 10, 23), date(2026, 10, 23))


def test_aware_timetable_timestamp_preserves_instant():
    item = TimetableLesson.from_hub("one", lesson(
        start="2026-10-23T08:00:00Z", end="2026-10-23T09:00:00Z", notes=None,
    ))
    assert item.starts_at.isoformat() == "2026-10-23T10:00:00+02:00"
    assert item.room is None
