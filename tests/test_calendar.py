import json
from datetime import date

import pytest

from infomentor_mcp.auth.session import AuthSession
from infomentor_mcp.client.infomentor import InfoMentorClient, UnexpectedInfoMentorResponse
from infomentor_mcp.client.models import Child

ENDPOINT = "https://hub.infomentor.se/calendarv2/calendarv2/getentries"
START, END = date(2026, 10, 22), date(2026, 10, 30)


@pytest.mark.parametrize("child_ids", [None, ["first", "first"], []])
def test_calendar_merges_sorts_filters_and_restores(monkeypatch, httpx_mock, child_ids):
    client = InfoMentorClient(AuthSession())
    switches = []
    monkeypatch.setattr(client, "_list_children", lambda: [
        Child(id="first", name="Ada"), Child(id="second", name="Bo", selected=True),
    ])
    monkeypatch.setattr(client, "_select_child", lambda id_, _: switches.append(id_))
    targets = ["first", "second"] if child_ids is None else list(dict.fromkeys(child_ids))
    for child in targets:
        httpx_mock.add_response(url=ENDPOINT, json=[{
            "id": id_, "title": "Synthetic", "text": "Description",
            "isAllDayEvent": all_day, "startDateFull": start, "endDateFull": end,
        } for id_, start, end, all_day in [
            (child, "2026-10-30T10:00:00", "2026-10-30T11:00:00", False),
            ("shared", "2026-10-21T00:00:00", "2026-10-22T23:59:59", True),
            ("outside", "2026-10-31T00:00:00", "2026-10-31T23:59:59", True),
        ]])
    try:
        items = client.get_calendar(START, END, child_ids)
        assert [item.id for item in items] == (["shared", *targets] if targets else [])
        if targets:
            assert items[0].child_ids == targets and items[0].all_day
            assert items[0].starts_at.isoformat().endswith("+02:00")
            assert items[-1].starts_at.isoformat().endswith("+01:00")  # Stockholm DST transition.
        assert switches == targets + ["second"]
        for request in httpx_mock.get_requests():
            assert request.method == "POST" and str(request.url) == ENDPOINT
            assert json.loads(request.content) == {
                "startDate": START.isoformat(), "endDate": END.isoformat(),
            }
    finally:
        client.close()


@pytest.mark.parametrize("payload", [{}, [{"id": "private-event"}], ["invalid"]])
def test_bad_calendar_response_fails_closed_and_restores(monkeypatch, httpx_mock, payload):
    client = InfoMentorClient(AuthSession())
    switches = []
    monkeypatch.setattr(client, "_list_children", lambda: [
        Child(id="first", name="Ada", selected=True),
    ])
    monkeypatch.setattr(client, "_select_child", lambda id_, _: switches.append(id_))
    httpx_mock.add_response(url=ENDPOINT, json=payload)
    try:
        with pytest.raises(UnexpectedInfoMentorResponse) as error:
            client.get_calendar(START, END)
        assert "private-event" not in str(error.value)
        assert switches == ["first", "first"]
    finally:
        client.close()
