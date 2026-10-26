import asyncio
import json
from contextlib import nullcontext
from datetime import date, timedelta
from pathlib import Path

import httpx
import keyring.errors
import pytest

from infomentor_mcp.app.mcp import McpApp
from infomentor_mcp.app.service import InfoMentorService
from infomentor_mcp.errors import (
    AuthenticationExpiredError,
    InfoMentorError,
    InvalidInput,
    SessionStorageError,
)
from infomentor_mcp.features.childcare import ChildcareWriter
from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.storage.session import AuthSession

ROOT = "https://hub.infomentor.se/TimeRegistration/TimeRegistration/"
DAY = date(2099, 1, 5)
WEEK = "2099-01-05"


class FailingSaveStore:
    state_dir = Path("unused-state")

    def __init__(self, error):
        self.error = error
        self.session = AuthSession()

    def locked(self, timeout=30):
        return nullcontext()

    def load(self):
        return self.session

    def save(self, session):
        raise self.error


@pytest.fixture
def one_child(fake_hub):
    fake_hub.children = {"one": "Synthetic"}
    fake_hub.selected = "one"
    return fake_hub


@pytest.fixture
def writer(one_child, hub):
    return ChildcareWriter(hub)


def registration_requests(httpx_mock):
    return [r for r in httpx_mock.get_requests() if r.url.path.startswith("/TimeRegistration/")]


def week_payload(**updates):
    row = {
        "date": WEEK + "T00:00:00", "timeRegistrationId": 7,
        "onLeave": False, "isSchoolClosed": False, "isLocked": False,
        "canEdit": True, "canEditComment": True,
        "startDateTime": WEEK + "T08:00:00", "endDateTime": WEEK + "T16:00:00",
    }
    row.update(updates)
    return {"startDate": WEEK, "endDate": "2099-01-11", "days": [row]}


@pytest.mark.parametrize("kind", ["times", "leave", "comment"])
@pytest.mark.parametrize("confirm", [False, True])
def test_preview_and_verified_save(writer, httpx_mock, kind, confirm):
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload())
    details = {"canEdit": True, "parentCommentId": 9, "userComment": "Old synthetic text"}
    if kind == "comment":
        httpx_mock.add_response(url=ROOT + "GetComments/", json=details)
    if confirm:
        endpoint = "SaveComment/" if kind == "comment" else "SaveTimeRegistrations/"
        httpx_mock.add_response(url=ROOT + endpoint, json={"success": True})
        if kind == "comment":
            httpx_mock.add_response(url=ROOT + "GetComments/", json={
                **details, "userComment": "New synthetic text",
            })
        else:
            httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(**(
                {"onLeave": True, "startDateTime": "", "endDateTime": ""} if kind == "leave"
                else {"endDateTime": WEEK + "T15:00:00"}
            )))
    if kind == "comment":
        result = writer.set_comment("one", [DAY], "New synthetic text", confirm=confirm)
    else:
        kwargs = {"on_leave": True} if kind == "leave" else {
            "start_time": "08:00", "end_time": "15:00",
        }
        result = writer.set_schedule("one", [DAY], confirm=confirm, **kwargs)
    assert result["status"] == ("completed" if confirm else "preview")
    assert result["items"][0]["status"] == ("verified" if confirm else "not_sent")
    writes = [r for r in httpx_mock.get_requests() if "/Save" in str(r.url)]
    assert len(writes) == int(confirm)
    if confirm:
        payload = json.loads(writes[0].content)
        if kind == "comment":
            assert payload == {"commentId": 9, "commentText": "New synthetic text",
                               "timeRegistrationId": 7}
        else:
            assert payload["series"] is None and len(payload["days"]) == 1
            row = payload["days"][0]
            assert row["isCommentUpdated"] is False
            assert row["registrationType"] == ("OnLeave" if kind == "leave" else "TimeReg")
            if kind == "leave":
                assert "startDateTime" not in row and "endDateTime" not in row


@pytest.mark.parametrize("updates", [
    {"isLocked": True}, {"canEdit": False}, {"isSchoolClosed": True},
])
def test_refuses_uneditable_days(writer, httpx_mock, updates):
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(**updates))
    with pytest.raises(InfoMentorError):
        writer.set_schedule("one", [DAY], on_leave=True, confirm=True)
    assert len(registration_requests(httpx_mock)) == 1


@pytest.mark.parametrize("failure", ["auth", "timeout", "rejected", "mismatch"])
def test_uncertain_write_is_not_replayed(writer, httpx_mock, failure):
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload())
    if failure in ("auth", "timeout"):
        error = (
            AuthenticationExpiredError("expired") if failure == "auth" else httpx.ReadTimeout("")
        )
        httpx_mock.add_exception(error, url=ROOT + "SaveTimeRegistrations/")
    else:
        httpx_mock.add_response(url=ROOT + "SaveTimeRegistrations/", json={
            "success": failure != "rejected",
        })
        if failure == "mismatch":
            httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload())
    result = writer.set_schedule("one", [DAY], on_leave=True, confirm=True)
    assert result["status"] == "incomplete"
    assert result["items"][0]["status"] == (
        "verification_mismatch" if failure == "mismatch" else "unknown"
    )
    assert sum("/Save" in str(r.url) for r in httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("kwargs", [
    {"on_leave": True, "start_time": "08:00"},
    {"start_time": "16:00", "end_time": "08:00"},
    {"start_time": "25:00", "end_time": "26:00"}, {},
])
def test_invalid_times_do_not_access_hub(writer, httpx_mock, kwargs):
    with pytest.raises(InvalidInput):
        writer.set_schedule("one", [DAY], **kwargs)
    assert not httpx_mock.get_requests()


def test_write_tool_defaults_and_annotations():
    tools = asyncio.run(McpApp().build().list_tools())
    for name in ("set_childcare_schedule", "set_childcare_comment"):
        tool = next(t for t in tools if t.name == name)
        assert not tool.input_schema["properties"]["confirm"]["default"]
        assert not tool.annotations.read_only_hint
        assert tool.annotations.destructive_hint


def test_all_dates_checked_before_saving(writer, httpx_mock):
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload())
    with pytest.raises(InfoMentorError, match="missing"):
        writer.set_schedule("one", [DAY, DAY + timedelta(days=1)],
                                      on_leave=True, confirm=True)
    assert len(registration_requests(httpx_mock)) == 1


def test_partial_batch_stops_after_uncertain_write(writer, httpx_mock):
    payload = week_payload()
    second = dict(payload["days"][0], date="2099-01-06T00:00:00",
                  startDateTime="2099-01-06T08:00:00", endDateTime="2099-01-06T16:00:00")
    payload["days"].append(second)
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=payload)
    httpx_mock.add_exception(httpx.ReadTimeout(""), url=ROOT + "SaveTimeRegistrations/")
    result = writer.set_schedule("one", [DAY, DAY + timedelta(days=1)],
                                          on_leave=True, confirm=True)
    assert [item["status"] for item in result["items"]] == ["unknown", "not_sent"]
    assert len(registration_requests(httpx_mock)) == 2


def test_restoration_auth_failure_does_not_replay_saved_write(fake_hub, hub, httpx_mock):
    fake_hub.children = {"original": "First", "one": "Second"}
    fake_hub.selected = "original"

    def save(request, child):
        fake_hub.rejected_switches.add("original")
        return {"success": True}

    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload())
    fake_hub.route("/TimeRegistration/TimeRegistration/SaveTimeRegistrations/", save)
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(
        onLeave=True, startDateTime="", endDateTime="",
    ))
    result = ChildcareWriter(hub).set_schedule("one", [DAY], on_leave=True, confirm=True)
    assert result["items"][0]["status"] == "verified"
    assert result["selection_restored"] is False
    assert fake_hub.read_by("/TimeRegistration/TimeRegistration/SaveTimeRegistrations/") == ["one"]


def test_comment_permission_and_care_hours(writer, httpx_mock):
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(
        canEditComment=False,
    ))
    with pytest.raises(InfoMentorError, match="not editable"):
        writer.set_comment("one", [DAY], "Synthetic", confirm=True)
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(
        schoolClosingTime=WEEK + "T17:00:00",
    ))
    with pytest.raises(InfoMentorError, match="opening hours"):
        writer.set_schedule("one", [DAY], start_time="08:00", end_time="18:00",
                                      confirm=True)
    assert all("/Save" not in str(r.url) for r in httpx_mock.get_requests())


@pytest.mark.parametrize("error_type", [OSError, ValueError, keyring.errors.KeyringError])
def test_verified_write_survives_session_save_failure_in_mcp(one_child, httpx_mock, error_type):
    store = FailingSaveStore(error_type("synthetic-secret-must-not-be-returned"))
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload())
    httpx_mock.add_response(url=ROOT + "SaveTimeRegistrations/", json={"success": True})
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(
        endDateTime=WEEK + "T15:00:00",
    ))
    # Without renewal, a replayed write would surface as authentication_required.
    service = InfoMentorService(store, hub_factory=Hub)
    server = McpApp(service).build()
    result = asyncio.run(server.call_tool("set_childcare_schedule", {
        "child_id": "one", "dates": [DAY.isoformat()], "start_time": "08:00",
        "end_time": "15:00", "confirm": True,
    })).structured_content
    assert result["status"] == "completed"
    assert result["items"][0]["status"] == "verified"
    assert result["warnings"][0]["code"] == "session_storage_error"
    assert "synthetic-secret" not in json.dumps(result)
    assert sum("/Save" in str(r.url) for r in httpx_mock.get_requests()) == 1


def test_uncertain_write_result_also_survives_session_save_failure(one_child, httpx_mock):
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload())
    httpx_mock.add_exception(httpx.ReadTimeout(""), url=ROOT + "SaveTimeRegistrations/")
    with Hub(FailingSaveStore(OSError("synthetic storage error"))) as hub:
        result = ChildcareWriter(hub).set_schedule("one", [DAY], on_leave=True, confirm=True)
    assert result["status"] == "incomplete" and result["items"][0]["status"] == "unknown"
    assert result["warnings"][0]["code"] == "session_storage_error"
    assert sum("/Save" in str(r.url) for r in httpx_mock.get_requests()) == 1


def test_failed_read_still_reports_session_save_failure(one_child, httpx_mock):
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(isLocked=True))
    with Hub(FailingSaveStore(OSError("synthetic storage error"))) as hub:
        with pytest.raises(SessionStorageError):
            ChildcareWriter(hub).set_schedule("one", [DAY], on_leave=True, confirm=True)
