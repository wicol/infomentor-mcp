import json

import pytest

from infomentor_mcp.errors import UnexpectedInfoMentorResponse
from infomentor_mcp.features.notifications import Notifications

PATH = "/NotificationApp/NotificationApp/GetNotifications"


def notifications(request, selected):
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
    return {"notifications": rows}


@pytest.mark.parametrize("child_ids", [None, ["first", "first"], []])
def test_notifications_merge_states_filter_selected_records_and_restore(
    fake_hub, hub, child_ids,
):
    fake_hub.route(PATH, notifications)
    targets = ["first", "second"] if child_ids is None else list(dict.fromkeys(child_ids))
    items = Notifications(hub).items(child_ids)
    assert [item.id for item in items] == (
        ["101", "102", "7"] if child_ids is None else ["101", "7"] if targets else []
    )
    if targets:
        assert items[-1].child_ids == targets
        assert items[-1].states == {id_: "New" if id_ == "first" else "Read" for id_ in targets}
    assert fake_hub.read_by(PATH) == targets
    assert fake_hub.selected == "second"
    for request in fake_hub.requests(PATH):
        assert json.loads(request.content) == {}  # No IDs/state updates or followed destinations.


@pytest.mark.parametrize("payload", [[], {}, {"notifications": [{"id": "secret-id"}]}])
def test_invalid_notifications_fail_closed_and_restore(fake_hub, hub, payload):
    fake_hub.route(PATH, lambda request, child: payload)
    with pytest.raises(UnexpectedInfoMentorResponse) as error:
        Notifications(hub).items(["first"])
    assert "secret" not in str(error.value)
    assert fake_hub.selected == "second"


def test_reused_selected_pupil_identity_is_rejected(fake_hub, hub):
    fake_hub.route(PATH, lambda request, child: {"notifications": [{
        "id": 7, "pupilIM2Id": 101, "pupilSourceId": "source-101",
        "currentlySelectedPupil": True, "state": "New",
    }]})
    with pytest.raises(UnexpectedInfoMentorResponse, match="ambiguous"):
        Notifications(hub).items()
    assert fake_hub.selected == "second"
