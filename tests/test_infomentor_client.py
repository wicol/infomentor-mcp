import pytest

from infomentor_mcp.auth.session import AuthSession, StoredCookie
from infomentor_mcp.client.infomentor import (
    InfoMentorClient,
    UnexpectedInfoMentorResponse,
    parse_child_ids,
)
from infomentor_mcp.client.models import Child

CHILDREN_PAGE = """
<script>IMHome.init = { selectedPupilName: 'One, Ada' };</script>
<script>var switches = ['/Account/PupilSwitcher/SwitchPupil/first'];</script>
"""


def test_parses_linked_child_ids_from_generated_routes() -> None:
    child_ids = parse_child_ids(
        "'/Account/PupilSwitcher/SwitchPupil/first',"
        "'/Account/PupilSwitcher/SwitchPupil/second'"
    )

    assert child_ids == ["first", "second"]


def test_rejects_unknown_child_before_switching(httpx_mock) -> None:
    httpx_mock.add_response(url="https://hub.infomentor.se/", text=CHILDREN_PAGE)
    httpx_mock.add_response(
        url="https://hub.infomentor.se/Account/PupilSwitcher/SwitchPupil/first",
        text=CHILDREN_PAGE,
        is_reusable=True,
    )
    session = AuthSession(
        cookies=[StoredCookie(name="IMHome", value="test", domain="hub.infomentor.se")]
    )
    client = InfoMentorClient(session)

    with pytest.raises(ValueError, match="not linked"):
        client.get_news(["unknown"])

    client.close()


def test_maps_news_to_the_requested_child(httpx_mock) -> None:
    httpx_mock.add_response(url="https://hub.infomentor.se/", text=CHILDREN_PAGE)
    httpx_mock.add_response(
        url="https://hub.infomentor.se/Account/PupilSwitcher/SwitchPupil/first",
        text=CHILDREN_PAGE,
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://hub.infomentor.se/Communication/News/GetNewsList",
        json={
            "items": [
                {
                    "id": 7,
                    "title": "Synthetic title",
                    "content": "<p>Synthetic content</p>",
                    "publishedDate": "2026-10-21T10:00:00",
                }
            ]
        },
    )
    session = AuthSession(
        cookies=[StoredCookie(name="IMHome", value="test", domain="hub.infomentor.se")]
    )
    client = InfoMentorClient(session)

    news = client.get_news(["first"])

    assert news[0].child_ids == ["first"]
    assert news[0].title == "Synthetic title"
    client.close()


def test_ignored_child_switch_rejected_before_read(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://hub.infomentor.se/Account/PupilSwitcher/SwitchPupil/second",
        text=CHILDREN_PAGE,
    )
    client = InfoMentorClient(AuthSession())
    client._expected_names = {"second": "Two, Bo"}
    try:
        with pytest.raises(UnexpectedInfoMentorResponse, match="did not confirm"):
            client._select_child("second", {"first", "second"})
        assert all(request.method != "POST" for request in httpx_mock.get_requests())
    finally:
        client.close()


@pytest.mark.parametrize("requested, expected", [
    (None, ["second", "shared", "first", "undated"]),
    (["first", "second", "first"], ["second", "shared", "first", "undated"]),
    (["first"], ["shared", "first", "undated"]),
    ([], []),
])
def test_news_merges_child_ids_and_sorts(monkeypatch, requested, expected):
    client = InfoMentorClient(AuthSession())
    children = [Child(id="first", name="Ada", selected=True), Child(id="second", name="Bo")]
    switches = []
    reads = []
    monkeypatch.setattr(client, "_list_children", lambda: children)
    monkeypatch.setattr(client, "_select_child", lambda child_id, _: switches.append(child_id))

    def read(child_id):
        reads.append(child_id)
        dates = {"first": "2026-10-01", "second": "2026-10-03", "shared": "2026-10-02"}
        ids = [child_id, "shared", "shared"] + (["undated"] if child_id == "first" else [])
        return [client._parse_news_item(child_id, {"id": id_, "publishedDate": dates.get(id_)})
                for id_ in ids]

    monkeypatch.setattr(client, "_get_selected_child_news", read)
    try:
        news = client.get_news(requested)
        assert [item.id for item in news] == expected
        expected_reads = list(dict.fromkeys(requested)) if requested is not None else [
            "first", "second"
        ]
        assert reads == expected_reads
        assert switches[-1] == "first"
        shared = next((item for item in news if item.id == "shared"), None)
        if shared:
            assert shared.child_ids == reads
    finally:
        client.close()


def test_duplicate_names_or_ignored_discovery_rejected(httpx_mock) -> None:
    page = CHILDREN_PAGE + "'/Account/PupilSwitcher/SwitchPupil/second'"
    httpx_mock.add_response(url="https://hub.infomentor.se/", text=page)
    for child_id in ("first", "second"):
        httpx_mock.add_response(
            url=f"https://hub.infomentor.se/Account/PupilSwitcher/SwitchPupil/{child_id}",
            text=page,
        )
    client = InfoMentorClient(AuthSession())
    try:
        with pytest.raises(UnexpectedInfoMentorResponse, match="ambiguous"):
            client.get_news()
        assert all(request.method != "POST" for request in httpx_mock.get_requests())
    finally:
        client.close()


def test_failed_first_read_restores_original_last_child(monkeypatch) -> None:
    client = InfoMentorClient(AuthSession())
    children = [
        Child(id="first", name="One, Ada"),
        Child(id="second", name="Two, Bo", selected=True),
    ]
    switches = []
    monkeypatch.setattr(client, "_list_children", lambda: children)
    monkeypatch.setattr(client, "_select_child", lambda child_id, _: switches.append(child_id))

    def failed_read(_):
        raise UnexpectedInfoMentorResponse("synthetic API failure")

    monkeypatch.setattr(client, "_get_selected_child_news", failed_read)
    try:
        with pytest.raises(UnexpectedInfoMentorResponse):
            client.get_news()
        assert switches == ["first", "second"]
    finally:
        client.close()
