import asyncio
import json
from contextlib import nullcontext
from datetime import date, timedelta
from pathlib import Path

import httpx
import keyring.errors
import pytest

from infomentor_mcp.auth.session import AuthSession
from infomentor_mcp.client.http import AuthenticationExpiredError, InfoMentorError
from infomentor_mcp.client.infomentor import InfoMentorClient
from infomentor_mcp.client.models import Child
from infomentor_mcp.server import create_server

ROOT = "https://hub.infomentor.se/TimeRegistration/TimeRegistration/"
DAY = date(2099, 1, 5)
WEEK = "2099-01-05"


class FailingSaveStore:
    fallback_path = Path("unused-session.json")
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
def client(monkeypatch):
    client = InfoMentorClient(AuthSession())
    monkeypatch.setattr(client, "_list_children", lambda: [
        Child(id="one", name="Synthetic", selected=True),
    ])
    monkeypatch.setattr(client, "_select_child", lambda *_: None)
    yield client
    client.close()


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
def test_preview_and_verified_save(client, httpx_mock, kind, confirm):
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
        result = client.set_childcare_comment("one", [DAY], "New synthetic text", confirm=confirm)
    else:
        kwargs = {"on_leave": True} if kind == "leave" else {
            "start_time": "08:00", "end_time": "15:00",
        }
        result = client.set_childcare_schedule("one", [DAY], confirm=confirm, **kwargs)
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
def test_refuses_uneditable_days(client, httpx_mock, updates):
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(**updates))
    with pytest.raises(InfoMentorError):
        client.set_childcare_schedule("one", [DAY], on_leave=True, confirm=True)
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("failure", ["auth", "timeout", "rejected", "mismatch"])
def test_uncertain_write_is_not_replayed(client, httpx_mock, monkeypatch, failure):
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
    monkeypatch.setattr(client, "_renew_session", lambda: pytest.fail("Write was replayed"))
    result = client.set_childcare_schedule("one", [DAY], on_leave=True, confirm=True)
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
def test_invalid_times_do_not_access_hub(client, httpx_mock, kwargs):
    assert client.set_childcare_schedule("one", [DAY], **kwargs)["error"]["code"] == "invalid_input"
    assert not httpx_mock.get_requests()


def test_write_tool_defaults_and_annotations():
    tools = asyncio.run(create_server().list_tools())
    for name in ("set_childcare_schedule", "set_childcare_comment"):
        tool = next(t for t in tools if t.name == name)
        assert not tool.input_schema["properties"]["confirm"]["default"]
        assert not tool.annotations.read_only_hint
        assert tool.annotations.destructive_hint


def test_all_dates_checked_before_saving(client, httpx_mock):
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload())
    with pytest.raises(InfoMentorError, match="missing"):
        client.set_childcare_schedule("one", [DAY, DAY + timedelta(days=1)],
                                      on_leave=True, confirm=True)
    assert len(httpx_mock.get_requests()) == 1


def test_partial_batch_stops_after_uncertain_write(client, httpx_mock):
    payload = week_payload()
    second = dict(payload["days"][0], date="2099-01-06T00:00:00",
                  startDateTime="2099-01-06T08:00:00", endDateTime="2099-01-06T16:00:00")
    payload["days"].append(second)
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=payload)
    httpx_mock.add_exception(httpx.ReadTimeout(""), url=ROOT + "SaveTimeRegistrations/")
    result = client.set_childcare_schedule("one", [DAY, DAY + timedelta(days=1)],
                                          on_leave=True, confirm=True)
    assert [item["status"] for item in result["items"]] == ["unknown", "not_sent"]
    assert len(httpx_mock.get_requests()) == 2


def test_restoration_auth_failure_does_not_replay_saved_write(client, httpx_mock, monkeypatch):
    def select(child_id, _):
        if child_id == "original":
            raise AuthenticationExpiredError("expired")

    monkeypatch.setattr(client, "_list_children", lambda: [
        Child(id="original", name="First", selected=True), Child(id="one", name="Second"),
    ])
    monkeypatch.setattr(client, "_select_child", select)
    monkeypatch.setattr(client, "_renew_session", lambda: pytest.fail("Write was replayed"))
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload())
    httpx_mock.add_response(url=ROOT + "SaveTimeRegistrations/", json={"success": True})
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(
        onLeave=True, startDateTime="", endDateTime="",
    ))
    result = client.set_childcare_schedule("one", [DAY], on_leave=True, confirm=True)
    assert result["items"][0]["status"] == "verified"
    assert result["selection_restored"] is False


def test_comment_permission_and_care_hours(client, httpx_mock):
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(
        canEditComment=False,
    ))
    with pytest.raises(InfoMentorError, match="not editable"):
        client.set_childcare_comment("one", [DAY], "Synthetic", confirm=True)
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(
        schoolClosingTime=WEEK + "T17:00:00",
    ))
    with pytest.raises(InfoMentorError, match="opening hours"):
        client.set_childcare_schedule("one", [DAY], start_time="08:00", end_time="18:00",
                                      confirm=True)
    assert all("/Save" not in str(r.url) for r in httpx_mock.get_requests())


@pytest.mark.parametrize("error_type", [OSError, ValueError, keyring.errors.KeyringError])
def test_verified_write_survives_session_save_failure_in_mcp(
    client, httpx_mock, monkeypatch, error_type,
):
    store = FailingSaveStore(error_type("synthetic-secret-must-not-be-returned"))
    client._session_store = client._lock_store = store
    monkeypatch.setattr(client, "_renew_session", lambda: pytest.fail("Write was replayed"))
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload())
    httpx_mock.add_response(url=ROOT + "SaveTimeRegistrations/", json={"success": True})
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload(
        endDateTime=WEEK + "T15:00:00",
    ))
    server = create_server(store, lambda _: client)
    result = asyncio.run(server.call_tool("set_childcare_schedule", {
        "child_id": "one", "dates": [DAY.isoformat()], "start_time": "08:00",
        "end_time": "15:00", "confirm": True,
    })).structured_content
    assert result["status"] == "completed"
    assert result["items"][0]["status"] == "verified"
    assert result["warnings"][0]["code"] == "session_storage_error"
    assert "synthetic-secret" not in json.dumps(result)
    assert sum("/Save" in str(r.url) for r in httpx_mock.get_requests()) == 1


def test_uncertain_write_result_also_survives_session_save_failure(client, httpx_mock, monkeypatch):
    store = FailingSaveStore(OSError("synthetic storage error"))
    client._session_store = client._lock_store = store
    monkeypatch.setattr(client, "_renew_session", lambda: pytest.fail("Write was replayed"))
    httpx_mock.add_response(url=ROOT + "GetTimeRegistrations/", json=week_payload())
    httpx_mock.add_exception(httpx.ReadTimeout(""), url=ROOT + "SaveTimeRegistrations/")
    result = client.set_childcare_schedule("one", [DAY], on_leave=True, confirm=True)
    assert result["status"] == "incomplete" and result["items"][0]["status"] == "unknown"
    assert result["warnings"][0]["code"] == "session_storage_error"
    assert sum("/Save" in str(r.url) for r in httpx_mock.get_requests()) == 1
