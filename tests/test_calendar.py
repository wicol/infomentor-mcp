import json
from datetime import date

import pytest

from infomentor_mcp.errors import UnexpectedInfoMentorResponse
from infomentor_mcp.features.calendar import Calendar

PATH = "/calendarv2/calendarv2/getentries"
START, END = date(2026, 10, 22), date(2026, 10, 30)


def events(child):
    return [{
        "id": id_, "title": "Synthetic", "text": "Description",
        "isAllDayEvent": all_day, "startDateFull": start, "endDateFull": end,
    } for id_, start, end, all_day in [
        (child, "2026-10-30T10:00:00", "2026-10-30T11:00:00", False),
        ("shared", "2026-10-21T00:00:00", "2026-10-22T23:59:59", True),
        ("outside", "2026-10-31T00:00:00", "2026-10-31T23:59:59", True),
    ]]


@pytest.mark.parametrize("child_ids", [None, ["first", "first"], []])
def test_calendar_merges_sorts_filters_and_restores(fake_hub, hub, child_ids):
    fake_hub.route(PATH, lambda request, child: events(child))
    targets = ["first", "second"] if child_ids is None else list(dict.fromkeys(child_ids))
    visited = fake_hub.visiting_order(targets)
    items = Calendar(hub).entries(START, END, child_ids)
    assert [item.id for item in items] == (["shared", *targets] if targets else [])
    if targets:
        assert items[0].child_ids == visited and items[0].all_day
        assert items[0].starts_at.isoformat().endswith("+02:00")
        assert items[-1].starts_at.isoformat().endswith("+01:00")  # Stockholm DST transition.
    assert fake_hub.read_by(PATH) == visited
    assert fake_hub.selected == "second"
    for request in fake_hub.requests(PATH):
        assert json.loads(request.content) == {
            "startDate": START.isoformat(), "endDate": END.isoformat(),
        }


@pytest.mark.parametrize("payload", [{}, [{"id": "private-event"}], ["invalid"]])
def test_bad_calendar_response_fails_closed_and_restores(fake_hub, hub, payload):
    fake_hub.route(PATH, lambda request, child: payload)
    with pytest.raises(UnexpectedInfoMentorResponse) as error:
        Calendar(hub).entries(START, END)
    assert "private-event" not in str(error.value)
    assert fake_hub.read_by(PATH) == ["second"]
    assert fake_hub.selected == "second"
