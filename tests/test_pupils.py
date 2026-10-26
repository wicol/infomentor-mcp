import httpx
import pytest

from infomentor_mcp.errors import UnexpectedInfoMentorResponse
from infomentor_mcp.features.news import News
from infomentor_mcp.hub.pupils import parse_child_ids

NEWS = "/Communication/News/GetNewsList"


def test_parses_linked_child_ids_from_generated_routes() -> None:
    child_ids = parse_child_ids(
        "'/Account/PupilSwitcher/SwitchPupil/first',"
        "'/Account/PupilSwitcher/SwitchPupil/second'"
    )

    assert child_ids == ["first", "second"]


def test_discovery_confirms_each_name_and_restores_the_original(fake_hub, hub):
    children = hub.run(hub.pupils.discover)
    assert [(c.id, c.name, c.selected) for c in children] == [
        ("first", "One, Ada", False), ("second", "Two, Bo", True),
    ]
    assert fake_hub.switches == ["first", "second"]
    assert fake_hub.selected == "second"


def test_rejects_unknown_child_before_switching(fake_hub, hub):
    fake_hub.route(NEWS, lambda request, child: {"items": []})
    with pytest.raises(ValueError, match="not linked"):
        News(hub).items(["unknown"])
    assert "unknown" not in fake_hub.switches
    assert not fake_hub.reads


def test_ignored_child_switch_rejected_before_read(fake_hub, hub):
    def news(request, child):
        fake_hub.ignored_switches.add("first")
        return {"items": [{"id": child}]}

    fake_hub.route(NEWS, news)
    with pytest.raises(UnexpectedInfoMentorResponse, match="did not confirm"):
        News(hub).items()
    assert fake_hub.read_by(NEWS) == ["second"]


@pytest.mark.parametrize("names, ignored", [
    ({"first": "One, Ada", "second": "One, Ada"}, set()),
    (None, {"second"}),
])
def test_duplicate_names_or_ignored_discovery_rejected(fake_hub, hub, names, ignored):
    fake_hub.children = names or fake_hub.children
    fake_hub.ignored_switches = ignored
    with pytest.raises(UnexpectedInfoMentorResponse, match="ambiguous"):
        News(hub).items()
    assert not fake_hub.reads


def test_failed_first_read_restores_original_selection(fake_hub, hub):
    fake_hub.selected = "first"
    fake_hub.route(NEWS, lambda request, child: httpx.Response(200, json={}))
    with pytest.raises(UnexpectedInfoMentorResponse):
        News(hub).items()
    assert fake_hub.read_by(NEWS) == ["second"]
    assert fake_hub.selected == "first"


@pytest.mark.parametrize("original, switches", [
    # Discovery ends on the last child; it is read first and the original restored once.
    ("first", ["first", "second", "first"]),
    ("second", ["first", "second", "first", "second"]),
])
def test_already_selected_children_are_not_switched_to_again(fake_hub, hub, original, switches):
    fake_hub.selected = original
    fake_hub.route(NEWS, lambda request, child: {"items": []})
    News(hub).items()
    assert fake_hub.read_by(NEWS) == ["second", "first"]
    assert fake_hub.switches == switches
    assert fake_hub.selected == original
