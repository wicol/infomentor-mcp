import asyncio
import json
from copy import deepcopy
from datetime import date
from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest

from infomentor_mcp.app.cli import Cli
from infomentor_mcp.app.mcp import McpApp
from infomentor_mcp.app.service import InfoMentorService
from infomentor_mcp.changes.store import ChangeStore
from infomentor_mcp.changes.tracker import ChangeTracker
from infomentor_mcp.errors import ChangeTrackingError
from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.storage.session import AuthSession, SessionStore


def batch(title="First", children=("one",), source="news"):
    return {"binding": "synthetic-binding", "errors": [], "snapshots": [
        {"child_id": child, "source": source, "items": [
            {"id": "entry", "title": title, "retrieved_at": "ignored", "raw": {"ignored": True},
             "child_ids": [child]},
        ]} for child in children
    ]}


def test_baseline_diff_and_independent_consumers_survive_restart(tmp_path):
    store = ChangeStore(tmp_path / "changes.sqlite3")
    assert not store.record(batch())["has_changes"]
    assert not store.record(batch())["has_changes"]
    assert store.record(batch("Second"))["new_changes"] == 1
    store = ChangeStore(store.path)
    page = store.get("notifier")
    change = page["changes"][0]
    assert change["changed_fields"] == ["title"]
    assert change["before"]["title"] == "First"
    assert change["after"]["title"] == "Second"
    assert "raw" not in change["after"] and "retrieved_at" not in change["after"]
    assert store.get("notifier") == page
    with pytest.raises(ChangeTrackingError):
        store.ack("calendar", page["next_cursor"])
    store.ack("notifier", page["next_cursor"])
    assert not store.get("notifier")["has_changes"]
    assert store.get("calendar")["changes"] == page["changes"]
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_pagination_and_ack_cannot_skip_undelivered_work(tmp_path):
    store = ChangeStore(tmp_path / "changes.sqlite3")
    store.record(batch())
    store.record(batch("Second"))
    store.record(batch("Third"))
    first = store.get(limit=1)
    assert first["has_more"]
    epoch, seq = first["next_cursor"].split(":")
    with pytest.raises(ChangeTrackingError):
        store.ack("default", f"{epoch}:{int(seq) + 1}")
    store.ack("default", first["next_cursor"])
    second = store.get(limit=1)
    assert not second["has_more"]
    assert second["changes"][0]["after"]["title"] == "Third"


def test_shared_changes_merge_children_but_not_channels(tmp_path):
    store = ChangeStore(tmp_path / "changes.sqlite3")
    original = batch(children=("one", "two"))
    original["snapshots"] += batch(children=("one", "two"), source="calendar")["snapshots"]
    store.record(original)
    updated = deepcopy(original)
    for snapshot in updated["snapshots"]:
        snapshot["items"][0]["title"] = "Updated"
    assert store.record(updated)["new_changes"] == 2
    assert all(c["child_ids"] == ["one", "two"] for c in store.get()["changes"])


def test_empty_failed_or_unselected_sources_never_mean_deletion(tmp_path):
    store = ChangeStore(tmp_path / "changes.sqlite3")
    original = batch(source="calendar")
    store.record(original)
    missing = deepcopy(original)
    missing["snapshots"][0]["items"] = []
    assert not store.record(missing)["has_changes"]
    failed = {"binding": original["binding"], "snapshots": [], "errors": [{"code": "fetch_failed"}]}
    assert store.record(failed)["status"] == "partial"
    assert not store.record(original)["has_changes"]
    assert store.record(batch("Updated", source="calendar"))["new_changes"] == 1


def test_attachment_hashes_includes_and_failed_reads(tmp_path):
    store = ChangeStore(tmp_path / "changes.sqlite3")
    original = batch()
    original["snapshots"][0]["items"][0]["attachments"] = [{
        "id": "file", "name": "file.pdf", "status": "extracted", "text": "Same text",
        "content_sha256": "hash-one", "truncated": False,
    }]
    store.record(original)
    failed = deepcopy(original)
    failed["snapshots"][0]["items"][0]["attachments"][0].update(
        status="fetch_failed", text=None, content_sha256=None,
    )
    assert not store.record(failed)["has_changes"]
    changed = deepcopy(original)
    changed["snapshots"][0]["items"][0]["attachments"][0]["content_sha256"] = "hash-two"
    assert store.record(changed)["new_changes"] == 1
    assert not store.record(changed, include_attachments=False)["has_changes"]
    assert not store.record(changed)["has_changes"]


def test_binding_reset_and_transaction_rollback(tmp_path):
    store = ChangeStore(tmp_path / "changes.sqlite3")
    store.record(batch())
    invalid = batch("Changed")
    invalid["snapshots"].append({"child_id": "one", "source": "news", "items": [{}]})
    with pytest.raises(KeyError):
        store.record(invalid)
    assert not store.get()["has_changes"]
    other = batch()
    other["binding"] = "another-authorization"
    with pytest.raises(ChangeTrackingError):
        store.record(other)
    store.record(batch("Second"))
    old_cursor = store.get()["next_cursor"]
    store.reset()
    assert not store.record(other)["has_changes"]
    with pytest.raises(ChangeTrackingError):
        store.ack("default", old_cursor)


def test_retention_never_silently_advances_an_existing_consumer(tmp_path):
    store = ChangeStore(tmp_path / "changes.sqlite3")
    store.record(batch())
    store.get("slow")
    store.record(batch("Second"))
    with store.connect() as db:
        db.execute("UPDATE changes SET created=0")
    store.record(batch("Third"))
    with pytest.raises(ChangeTrackingError, match="expired"):
        store.get("slow")
    assert store.get("new")["changes"][0]["after"]["title"] == "Third"


NEWS = "/Communication/News/GetNewsList"
CALENDAR = "/calendarv2/calendarv2/getentries"
DOWNLOAD = "/Resources/Resource/Download/123"


NEWS = "/Communication/News/GetNewsList"
CALENDAR = "/calendarv2/calendarv2/getentries"
DOWNLOAD = "/Resources/Resource/Download/123"
ATTACHMENT = DOWNLOAD + "?api=IM2&ModuleType=NewsItem&ConnectionId=post"
DAYS = (date(2026, 1, 1), date(2026, 1, 2))


class CountingRenewal:
    def __init__(self):
        self.calls = 0

    def renew(self, current):
        self.calls += 1
        return AuthSession(mobile_binding=current.mobile_binding)


@pytest.fixture
def tracking_hub(fake_hub):
    fake_hub.children = {"one": "One", "two": "Two"}
    fake_hub.selected = "one"
    return fake_hub


@pytest.fixture
def bound_store():
    store = SessionStore()
    store.save(AuthSession(mobile_binding="synthetic-binding"))
    return store


@pytest.fixture
def tracker(tracking_hub, bound_store, tmp_path):
    with Hub(bound_store) as hub:
        yield ChangeTracker(hub, ChangeStore(tmp_path / "changes.sqlite3"))


def news_items(title="News", attachment=False):
    def respond(request, child):
        return {"items": [{"id": "post", "title": title, "content": "Synthetic", "attachments": [
            {"title": "weekly.docx", "url": ATTACHMENT},
        ] if attachment else []}]}
    return respond


def test_collection_is_child_first_with_single_discovery(tracking_hub, tracker):
    tracking_hub.route(NEWS, lambda request, child: {"items": []})
    tracking_hub.route(CALENDAR, lambda request, child: [])
    result = tracker.collect(["news", "calendar"], None, *DAYS, request_delay_seconds=0)
    assert tracking_hub.reads == [
        (NEWS, "two"), (CALENDAR, "two"), (NEWS, "one"), (CALENDAR, "one"),
    ]
    assert len(tracking_hub.requests("/")) == 1
    assert tracking_hub.switches == ["one", "two", "one"]
    assert tracking_hub.selected == "one"
    assert len(result["snapshots"]) == 4


def test_partial_errors_restore_child_and_preserve_other_sources(tracking_hub, tracker):
    tracking_hub.route(NEWS, lambda request, child: httpx.Response(503))
    tracking_hub.route(CALENDAR, lambda request, child: [])
    result = tracker.collect(["news", "calendar"], None, *DAYS, request_delay_seconds=0)
    assert [(e["child_id"], e["source"]) for e in result["errors"]] == [
        ("two", "news"), ("one", "news"),
    ]
    assert [s["source"] for s in result["snapshots"]] == ["calendar", "calendar"]
    assert tracking_hub.selected == "one"


def test_renewal_replays_collection_before_persisting(tracking_hub, bound_store, tmp_path):
    reads = []

    def news(request, child):
        reads.append(child)
        return httpx.Response(401) if len(reads) == 1 else {"items": []}

    tracking_hub.route(NEWS, news)
    renewal = CountingRenewal()
    with Hub(bound_store, renewal) as hub:
        result = ChangeTracker(hub, ChangeStore(tmp_path / "changes.sqlite3")).collect(
            ["news"], None, *DAYS, request_delay_seconds=0,
        )
    assert renewal.calls == 1
    assert len(tracking_hub.requests("/")) == 2
    assert reads == ["two", "two", "one"]
    assert len(result["snapshots"]) == 2 and not result["errors"]
    assert result["binding"] == "synthetic-binding"


def test_request_pacing_applies_to_each_http_request(tracking_hub, tracker, monkeypatch):
    sleeps = []
    monkeypatch.setattr("infomentor_mcp.changes.tracker.time.sleep", sleeps.append)
    monkeypatch.setattr("infomentor_mcp.changes.tracker.time.monotonic", lambda: 10)
    tracking_hub.route(NEWS, lambda request, child: {"items": []})
    tracker.collect(["news"], ["one"], *DAYS, request_delay_seconds=0.5)
    requests = tracking_hub.httpx_mock.get_requests()
    assert len(requests) > 2
    assert sleeps == [0.5] * (len(requests) - 1)
    assert not tracker.hub.transport.client.event_hooks["request"]


def test_mcp_change_delivery_does_not_need_a_live_session(tmp_path):
    changes = ChangeStore(tmp_path / "changes.sqlite3")
    changes.record(batch())
    changes.record(batch("Updated"))
    server = McpApp(InfoMentorService(SessionStore(tmp_path / "session.json"), changes)).build()
    result = asyncio.run(server.call_tool("get_changes", {"consumer": "agent"})).structured_content
    assert result["has_changes"]
    ack = asyncio.run(server.call_tool("ack_changes", {
        "consumer": "agent", "cursor": result["next_cursor"],
    })).structured_content
    assert ack["status"] == "ok"
    assert "synthetic-binding" not in json.dumps(result)


def test_sync_returns_old_pending_work_on_unchanged_fetch(tracking_hub, tracker):
    tracking_hub.route(CALENDAR, lambda request, child: [])
    title = "First"
    tracking_hub.route(NEWS, lambda request, child: news_items(title)(request, child))

    def run():
        return tracker.sync(consumer="agent", request_delay_seconds=0)

    assert not run()["has_changes"]
    title = "Updated"
    assert run()["new_changes"] == 1
    result = run()
    assert result["new_changes"] == 0 and result["has_changes"]
    tracker.changes.ack("agent", result["next_cursor"])
    assert not run()["has_changes"]


def test_cli_sync_get_ack_and_reset_are_json(tracking_hub, bound_store, capsys):
    tracking_hub.route(CALENDAR, lambda request, child: [])
    title = "First"
    tracking_hub.route(NEWS, lambda request, child: news_items(title)(request, child))
    cli = Cli(bound_store)
    args = ["sync", "--consumer", "agent", "--request-delay-seconds", "0"]
    assert cli.run(args) == 0
    assert not json.loads(capsys.readouterr().out)["has_changes"]
    title = "Changed"
    assert cli.run([*args, "--sources", "news"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["has_changes"] and result["new_changes"] == 1
    assert cli.run(["changes", "get", "--consumer", "agent"]) == 0
    assert json.loads(capsys.readouterr().out)["changes"] == result["changes"]
    assert cli.run(["changes", "ack", "--consumer", "agent",
                    "--cursor", result["next_cursor"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert cli.run(["changes", "reset", "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "reset"


def test_mcp_sync_defaults_and_partial_result(tracking_hub, bound_store, monkeypatch):
    sleeps = []
    monkeypatch.setattr("infomentor_mcp.changes.tracker.time.sleep", sleeps.append)
    tracking_hub.route(NEWS, news_items(attachment=True))
    tracking_hub.route(CALENDAR, lambda request, child: httpx.Response(503))
    tracking_hub.route(DOWNLOAD, lambda request, child: httpx.Response(200, content=b"PK"),
                       method="GET")
    server = McpApp(InfoMentorService(bound_store)).build()
    result = asyncio.run(server.call_tool("sync_changes", {})).structured_content
    assert result["status"] == "partial"
    assert [(e["child_id"], e["source"]) for e in result["errors"]] == [
        ("two", "calendar"), ("one", "calendar"),
    ]
    assert tracking_hub.read_by(DOWNLOAD) == ["two"]  # Attachments by default.
    assert sleeps and all(0 < delay <= 1.0 for delay in sleeps)  # Paced by default.


def test_invalid_sources_delay_and_dates_make_no_requests(tracking_hub, tracker):
    for sources, start, end, delay in [
        ([], *DAYS, 1),
        (["notifications"], *DAYS, 1),
        (["news"], date(2026, 1, 2), date(2026, 1, 1), 1),
        (["news"], *DAYS, float("nan")),
    ]:
        with pytest.raises(ValueError):
            tracker.collect(sources, None, start, end, request_delay_seconds=delay)
    assert not tracking_hub.httpx_mock.get_requests()


def test_binding_mismatch_is_rejected_before_network(tracking_hub, tracker):
    original = batch()
    original["binding"] = "different-authorization"
    tracker.changes.record(original)
    with pytest.raises(ChangeTrackingError) as error:
        tracker.sync()
    assert error.value.code == "tracking_reset_required"
    assert not tracking_hub.httpx_mock.get_requests()


def test_http_collection_and_disabled_includes(tracking_hub, tracker):
    tracking_hub.route(NEWS, news_items(attachment=True))
    tracking_hub.route(CALENDAR, lambda request, child: [])
    result = tracker.collect(["news", "calendar"], None, *DAYS, False, 0)
    assert not result["errors"] and len(result["snapshots"]) == 4
    assert result["snapshots"][0]["items"][0]["title"] == "News"
    assert result["snapshots"][0]["items"][0]["attachments"][0]["status"] == "available"
    assert not result["attachment_checks"]
    assert not tracking_hub.requests(DOWNLOAD)


def docx(text):
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
        ))
    return content.getvalue()


def test_settled_attachment_reads_are_reused_across_children_and_syncs(tracking_hub, tracker):
    tracking_hub.route(NEWS, lambda request, child: {"items": [{"id": "post", "attachments": [{
        "title": f"{id_}.docx",
        "url": f"/Resources/Resource/Download/{id_}?api=IM2&ModuleType=NewsItem&ConnectionId=post",
    } for id_ in ("123", "124")]}]})
    downloads = []

    def download(request, child):
        file_id = request.url.path.rsplit("/", 1)[1]
        downloads.append(file_id)
        if downloads == ["123", "124"]:
            return httpx.Response(503)
        return httpx.Response(200, content=docx(f"File {file_id}"))

    for id_ in ("123", "124"):
        tracking_hub.route(f"/Resources/Resource/Download/{id_}", download, method="GET")

    def sync():
        result = tracker.sync(sources=["news"], consumer="agent", request_delay_seconds=0)
        return {(c["child_id"], c["attachment_id"], c["status"])
                for c in result["attachment_checks"]}, result

    checks, _ = sync()
    # Files shared between children are read once; a failed read is not retried in a sync.
    assert downloads == ["123", "124"]
    assert checks == {(child, id_, status) for child in ("one", "two")
                      for id_, status in (("123", "extracted"), ("124", "fetch_failed"))}
    sync()
    assert downloads == ["123", "124", "124"]  # Only the failed read is retried.
    checks, result = sync()
    assert downloads == ["123", "124", "124"]
    assert result["new_changes"] == 0
    assert checks == {
        (child, id_, "extracted") for child in ("one", "two") for id_ in ("123", "124")
    }
    post = tracker.changes.get("agent")["changes"][-1]["after"]
    assert [a["text"] for a in post["attachments"]] == ["File 123", "File 124"]


def test_extraction_budget_changes_are_not_content_changes(tmp_path):
    store = ChangeStore(tmp_path / "changes.sqlite3")
    original = batch()
    file = {"id": "file", "name": "file.pdf", "content_sha256": "same-hash",
            "status": "extracted", "text": "Long text", "truncated": False}
    original["snapshots"][0]["items"][0]["attachments"] = [file]
    store.record(original)
    limited = deepcopy(original)
    limited["snapshots"][0]["items"][0]["attachments"][0].update(text="Long", truncated=True)
    assert not store.record(limited)["has_changes"]
