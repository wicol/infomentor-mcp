import json
from datetime import date, timedelta

import httpx
import pytest

from infomentor_mcp.errors import UnexpectedInfoMentorResponse
from infomentor_mcp.features.childcare import Childcare

WEEKS = "/TimeRegistration/TimeRegistration/GetTimeRegistrations/"
COMMENTS = "/TimeRegistration/TimeRegistration/GetComments/"


def week_of(request):
    return date.fromisoformat(json.loads(request.content)["date"][:10])


def registrations(request, child):
    week = week_of(request)
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
    return {"startDate": week.isoformat(), "endDate": (week + timedelta(days=6)).isoformat(),
            "days": rows}


@pytest.fixture
def one_child(fake_hub):
    fake_hub.children = {"first": "One, Ada"}
    fake_hub.selected = "first"
    return fake_hub


@pytest.mark.parametrize("child_ids", [None, ["first", "first"], []])
def test_childcare_reads_requested_weeks_and_keeps_children_separate(fake_hub, hub, child_ids):
    fake_hub.route(WEEKS, registrations)
    targets = ["first", "second"] if child_ids is None else list(dict.fromkeys(child_ids))
    items = Childcare(hub).schedule(
        date(2026, 10, 23), date(2026, 10, 26), child_ids, include_comments=False
    )
    assert [(r.date.day, r.child_id) for r in items] == [
        (day, child) for day in (23, 26) for child in targets
    ]
    if targets:
        assert items[0].starts_at.isoformat().endswith("+02:00")
        assert items[-1].on_leave and items[-1].starts_at is None
        assert items[-1].opens_at.isoformat().endswith("+01:00")
        assert items[-1].has_unread_comments
    visited = fake_hub.visiting_order(targets)
    assert fake_hub.read_by(WEEKS) == [child for child in visited for _ in range(2)]
    assert fake_hub.selected == "second"
    assert [json.loads(r.content) for r in fake_hub.requests(WEEKS)] == [{
        "date": f"2026-10-{week}T00:00:00.000Z", "showNextWeekIfNoMoreSchoolDays": False,
    } for _ in visited for week in (19, 26)]


@pytest.mark.parametrize("mode", ["default", "skip", "invalid", "http_failure"])
def test_childcare_comments_only_open_returned_page_and_report_read_receipts(
    one_child, hub, mode,
):
    one_child.route(WEEKS, lambda request, child: {
        "startDate": "2026-10-19", "endDate": "2026-10-25", "days": [
            {"date": f"2026-10-{day}", "onLeave": False, "isSchoolClosed": False,
             "hasComments": True, "hasUnreadComments": True} for day in (19, 20)
        ],
    })
    if mode != "skip":
        one_child.route(COMMENTS, lambda request, child: httpx.Response(
            503 if mode == "http_failure" else 200,
            json={} if mode == "invalid" else {
                "teacherComment": "Synthetic staff reply", "teacherName": "Staff",
                "teacherCommentDate": "2026-10-20T09:00:00",
                "userComment": "Synthetic request",
            },
        ))
    kwargs = {"include_comments": False} if mode == "skip" else {}
    items = Childcare(hub).schedule(
        date(2026, 10, 19), date(2026, 10, 20), offset=1, limit=1, **kwargs,
    )
    assert len(items) == 1 and items[0].date.day == 20
    item = items[0]
    if mode == "default":
        assert item.comments_status == "loaded" and item.comments_marked_read
        assert [c.author_type for c in item.comments] == ["staff", "guardian"]
        assert not item.has_unread_comments
    elif mode == "skip":
        assert item.comments_status == "not_requested" and item.has_unread_comments
        assert item.comments_marked_read is False
    else:
        assert item.comments_status == (
            "invalid_response" if mode == "invalid" else "fetch_failed"
        )
        assert item.comments_marked_read is None
    comment_reads = one_child.requests(COMMENTS)
    assert len(comment_reads) == (0 if mode == "skip" else 1)
    if comment_reads:
        assert json.loads(comment_reads[0].content) == {"date": "2026-10-20T00:00:00"}
        assert one_child.read_by(COMMENTS) == ["first"]
    assert one_child.selected == "first"


@pytest.mark.parametrize("payload", [
    {"startDate": "2026-10-26", "endDate": "2026-11-01", "days": []},
    {"startDate": "2026-10-19", "endDate": "2026-10-25", "days": [{"date": "secret"}]},
])
def test_childcare_refuses_wrong_week_or_invalid_day_and_restores(fake_hub, hub, payload):
    fake_hub.route(WEEKS, lambda request, child: payload)
    with pytest.raises(UnexpectedInfoMentorResponse) as error:
        Childcare(hub).schedule(date(2026, 10, 22), date(2026, 10, 23))
    assert "secret" not in str(error.value)
    assert fake_hub.read_by(WEEKS) == ["second"]
    assert fake_hub.selected == "second"
