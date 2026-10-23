from datetime import date, datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import pytest

from infomentor_mcp.auth.session import AuthSession
from infomentor_mcp.client.http import AuthenticationExpiredError, InfoMentorError
from infomentor_mcp.client.infomentor import InfoMentorClient, UnexpectedInfoMentorResponse
from infomentor_mcp.client.models import Child

ENDPOINT = "https://hub.infomentor.se/timetable/timetable/gettimetablelist"


def lesson(day="2026-10-23", **overrides):
    return {
        "title": "Synthetic subject", "start": day + "T10:00:00",
        "end": day + "T11:00:00", "allDay": False, "details": "Synthetic group",
        "notes": {"roomInfo": "Room A", "tutors": "Teacher A, Teacher B",
                  "timetableNotes": "Synthetic change"}, **overrides,
    }


@pytest.mark.parametrize("child_ids", [None, ["first", "first"], []])
def test_timetable_form_dates_dst_details_and_separate_children(
    monkeypatch, httpx_mock, child_ids,
):
    client = InfoMentorClient(AuthSession())
    switches = []
    monkeypatch.setattr(client, "_list_children", lambda: [
        Child(id="first", name="Ada"), Child(id="second", name="Bo", selected=True),
    ])
    monkeypatch.setattr(client, "_select_child", lambda id_, _: switches.append(id_))
    targets = ["first", "second"] if child_ids is None else list(dict.fromkeys(child_ids))
    for _ in targets:
        # Identical entries must remain separate, even for the same pupil.
        httpx_mock.add_response(url=ENDPOINT, json=[lesson("2026-10-26"), lesson(), lesson()])
    try:
        items = client.get_timetable(date(2026, 10, 23), date(2026, 10, 26), child_ids)
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
        assert switches == targets + ["second"]
        requests = httpx_mock.get_requests()
        assert len(requests) == len(targets)  # One request per child, even across DST.
        offset = datetime.now(ZoneInfo("Europe/Stockholm")).utcoffset()
        for request in requests:
            assert request.method == "POST"
            assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
            assert parse_qs(request.content.decode()) == {
                "start": ["2026-10-23"], "end": ["2026-10-27"],
                "UTCOffset": [str(-int(offset.total_seconds() // 60))],
            }
    finally:
        client.close()


@pytest.mark.parametrize("payload", [
    {}, ["private-invalid"], [lesson(start="private-invalid")],
    [lesson(end="2026-10-23T09:00:00")], [lesson(notes=[])],
    [lesson(notes={"tutors": ["private-invalid"]})], [lesson("2026-10-24")],
])
def test_invalid_timetable_fails_closed_and_restores(monkeypatch, httpx_mock, payload):
    client = InfoMentorClient(AuthSession())
    switches = []
    monkeypatch.setattr(client, "_list_children", lambda: [
        Child(id="one", name="Ada", selected=True),
    ])
    monkeypatch.setattr(client, "_select_child", lambda id_, _: switches.append(id_))
    httpx_mock.add_response(url=ENDPOINT, json=payload)
    try:
        with pytest.raises(UnexpectedInfoMentorResponse) as error:
            client.get_timetable(date(2026, 10, 23), date(2026, 10, 23))
        assert "private-invalid" not in str(error.value)
        assert switches == ["one", "one"]
    finally:
        client.close()


@pytest.mark.parametrize("status", [200, 401, 403])
def test_empty_schedule_is_not_an_auth_failure_and_form_uses_auth_guards(
    monkeypatch, httpx_mock, status,
):
    client = InfoMentorClient(AuthSession())
    monkeypatch.setattr(client, "_list_children", lambda: [
        Child(id="one", name="Ada", selected=True),
    ])
    monkeypatch.setattr(client, "_select_child", lambda *_: None)
    httpx_mock.add_response(url=ENDPOINT, status_code=status, json=[])
    try:
        if status == 200:
            assert client.get_timetable(date(2026, 10, 23), date(2026, 10, 23)) == []
        elif status == 401:
            # Without a session store, the standard renewal path requires login.
            with pytest.raises(AuthenticationExpiredError):
                client.get_timetable(date(2026, 10, 23), date(2026, 10, 23))
        else:
            with pytest.raises(InfoMentorError, match="403"):
                client.get_timetable(date(2026, 10, 23), date(2026, 10, 23))
    finally:
        client.close()


def test_aware_timetable_timestamp_preserves_instant():
    item = InfoMentorClient._parse_timetable_lesson("one", lesson(
        start="2026-10-23T08:00:00Z", end="2026-10-23T09:00:00Z", notes=None,
    ))
    assert item.starts_at.isoformat() == "2026-10-23T10:00:00+02:00"
    assert item.room is None
