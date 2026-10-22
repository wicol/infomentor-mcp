import json
from datetime import date, timedelta

import pytest

from infomentor_mcp.auth.session import AuthSession
from infomentor_mcp.client.infomentor import InfoMentorClient, UnexpectedInfoMentorResponse
from infomentor_mcp.client.models import Child

ENDPOINT = "https://hub.infomentor.se/TimeRegistration/TimeRegistration/GetTimeRegistrations/"


@pytest.mark.parametrize("child_ids", [None, ["first", "first"], []])
def test_childcare_reads_requested_weeks_and_keeps_children_separate(
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
        for week in (date(2026, 10, 19), date(2026, 10, 26)):
            rows = []
            for offset in range(5):
                day = (week + timedelta(days=offset)).isoformat()
                leave = week.day == 26
                rows.append({
                    "date": day + "T00:00:00", "timeRegistrationId": 7,
                    "onLeave": leave, "isSchoolClosed": False, "hasUnreadComments": True,
                    "startDateTime": "" if leave else day + "T08:00:00",
                    "endDateTime": "" if leave else day + "T16:00:00",
                    "schoolOpeningTime": day + "T07:00:00",
                    "schoolClosingTime": day + "T18:00:00",
                })
            httpx_mock.add_response(url=ENDPOINT, json={
                "startDate": week.isoformat(),
                "endDate": (week + timedelta(days=6)).isoformat(), "days": rows,
            })
    try:
        items = client.get_childcare_schedule(date(2026, 10, 23), date(2026, 10, 26), child_ids)
        assert [(r.date.day, r.child_id) for r in items] == [
            (day, child) for day in (23, 26) for child in targets
        ]
        if targets:
            assert items[0].starts_at.isoformat().endswith("+02:00")
            assert items[-1].on_leave and items[-1].starts_at is None
            assert items[-1].opens_at.isoformat().endswith("+01:00")
            assert items[-1].has_unread_comments
        assert switches == targets + ["second"]
        for index, request in enumerate(httpx_mock.get_requests()):
            assert request.method == "POST" and str(request.url) == ENDPOINT
            assert json.loads(request.content) == {
                "date": f"2026-10-{19 if index % 2 == 0 else 26}T00:00:00.000Z",
                "showNextWeekIfNoMoreSchoolDays": False,
            }
    finally:
        client.close()


@pytest.mark.parametrize("payload", [
    {"startDate": "2026-10-26", "endDate": "2026-11-01", "days": []},
    {"startDate": "2026-10-19", "endDate": "2026-10-25", "days": [{"date": "secret"}]},
])
def test_childcare_refuses_wrong_week_or_invalid_day_and_restores(
    monkeypatch, httpx_mock, payload,
):
    client = InfoMentorClient(AuthSession())
    switches = []
    monkeypatch.setattr(client, "_list_children", lambda: [
        Child(id="first", name="Ada", selected=True),
    ])
    monkeypatch.setattr(client, "_select_child", lambda id_, _: switches.append(id_))
    httpx_mock.add_response(url=ENDPOINT, json=payload)
    try:
        with pytest.raises(UnexpectedInfoMentorResponse) as error:
            client.get_childcare_schedule(date(2026, 10, 22), date(2026, 10, 23))
        assert "secret" not in str(error.value)
        assert switches == ["first", "first"]
    finally:
        client.close()
