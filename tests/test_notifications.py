import json

import pytest

from infomentor_mcp.auth.session import AuthSession
from infomentor_mcp.client.infomentor import InfoMentorClient, UnexpectedInfoMentorResponse
from infomentor_mcp.client.models import Child

ENDPOINT = "https://hub.infomentor.se/NotificationApp/NotificationApp/GetNotifications"


@pytest.fixture
def selected_client(monkeypatch):
    client = InfoMentorClient(AuthSession())
    switches = []
    monkeypatch.setattr(client, "_list_children", lambda: [
        Child(id="first", name="Ada"), Child(id="second", name="Bo", selected=True),
    ])
    monkeypatch.setattr(client, "_select_child", lambda id_, _: switches.append(id_))
    yield client, switches
    client.close()


@pytest.mark.parametrize("child_ids", [None, ["first", "first"], []])
def test_notifications_merge_states_filter_selected_records_and_restore(
    selected_client, httpx_mock, child_ids,
):
    client, switches = selected_client
    targets = ["first", "second"] if child_ids is None else list(dict.fromkeys(child_ids))
    for selected in targets:
        rows = []
        for child, pupil_id in [("first", 101), ("second", 102)]:
            for id_, date in [(7, "2026-10-20"), (pupil_id, "2026-10-22")]:
                rows.append({
                    "id": id_, "title": "Synthetic", "subTitle": "Details",
                    "dateSent": "2026-10-01", "orderDate": date, "appType": "News",
                    "state": "New" if child == "first" else "Read",
                    "pupilIM2Id": pupil_id, "pupilSourceId": f"upstream-{pupil_id}",
                    "currentlySelectedPupil": child == selected,
                    "url": "/#/communication/news/123",
                })
        httpx_mock.add_response(url=ENDPOINT, json={"notifications": rows})
    items = client.get_notifications(child_ids)
    assert [item.id for item in items] == (
        ["101", "102", "7"] if child_ids is None else ["101", "7"] if targets else []
    )
    if targets:
        assert items[-1].child_ids == targets
        assert items[-1].states == {id_: "New" if id_ == "first" else "Read" for id_ in targets}
    assert switches == targets + ["second"]
    for request in httpx_mock.get_requests():
        assert request.method == "POST" and str(request.url) == ENDPOINT
        assert json.loads(request.content) == {}  # No IDs/state updates or followed destinations.


@pytest.mark.parametrize("payload", [[], {}, {"notifications": [{"id": "secret-id"}]}])
def test_invalid_notifications_fail_closed_and_restore(selected_client, httpx_mock, payload):
    client, switches = selected_client
    httpx_mock.add_response(url=ENDPOINT, json=payload)
    with pytest.raises(UnexpectedInfoMentorResponse) as error:
        client.get_notifications(["first"])
    assert "secret" not in str(error.value)
    assert switches == ["first", "second"]


def test_reused_selected_pupil_identity_is_rejected(selected_client, httpx_mock):
    client, switches = selected_client
    httpx_mock.add_response(url=ENDPOINT, is_reusable=True, json={"notifications": [{
        "id": 7, "pupilIM2Id": 101, "pupilSourceId": "source-101",
        "currentlySelectedPupil": True, "state": "New",
    }]})
    with pytest.raises(UnexpectedInfoMentorResponse, match="ambiguous"):
        client.get_notifications()
    assert switches[-1] == "second"
