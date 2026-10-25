import asyncio
import json
from copy import deepcopy
from datetime import date

import pytest

from infomentor_mcp.auth.session import AuthSession, SessionStore
from infomentor_mcp.changes import ChangeStore, ChangeTrackingError, sync
from infomentor_mcp.client.http import AuthenticationExpiredError, InfoMentorError
from infomentor_mcp.client.infomentor import InfoMentorClient
from infomentor_mcp.client.models import Child
from infomentor_mcp.server import create_server


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


@pytest.fixture
def collecting_client(monkeypatch):
    client = InfoMentorClient(AuthSession(mobile_binding="synthetic-binding"))
    log = []
    children = [Child(id="one", name="One", selected=True), Child(id="two", name="Two")]
    monkeypatch.setattr(client, "_list_children", lambda: log.append("discover") or children)
    monkeypatch.setattr(client, "_select_child", lambda id_, _: log.append(f"select:{id_}"))
    monkeypatch.setattr(client, "_get_selected_child_news",
                        lambda id_: log.append(f"news:{id_}") or [])
    monkeypatch.setattr(client, "_get_selected_child_calendar",
                        lambda id_, *_: log.append(f"calendar:{id_}") or [])
    yield client, log
    client.close()


def test_collection_is_child_first_with_single_discovery(collecting_client):
    client, log = collecting_client
    result = client.collect_changes(["news", "calendar"], None, date(2026, 1, 1), date(2026, 1, 2))
    assert log == ["discover", "select:one", "news:one", "calendar:one",
                   "select:two", "news:two", "calendar:two", "select:one"]
    assert len(result["snapshots"]) == 4
    assert not client._http.client.event_hooks["request"]


def test_partial_errors_restore_child_and_preserve_other_sources(collecting_client, monkeypatch):
    client, log = collecting_client

    def fail(_):
        raise InfoMentorError("withheld")

    monkeypatch.setattr(client, "_get_selected_child_news", fail)
    result = client.collect_changes(["news", "calendar"], None, date(2026, 1, 1), date(2026, 1, 2))
    assert len(result["errors"]) == 2 and len(result["snapshots"]) == 2
    assert log[-1] == "select:one"


def test_renewal_replays_collection_before_persisting(collecting_client, monkeypatch):
    client, log = collecting_client
    calls = []

    def news(id_):
        calls.append(id_)
        if len(calls) == 1:
            raise AuthenticationExpiredError("Expired")
        return []

    monkeypatch.setattr(client, "_get_selected_child_news", news)
    monkeypatch.setattr(client, "_renew_session", lambda: log.append("renew"))
    result = client.collect_changes(["news"], None, date(2026, 1, 1), date(2026, 1, 2))
    assert len(result["snapshots"]) == 2
    assert log.count("discover") == 2 and log.count("renew") == 1


def test_request_pacing_applies_to_each_http_request(collecting_client, monkeypatch, httpx_mock):
    client, _ = collecting_client
    sleeps = []
    monkeypatch.setattr("infomentor_mcp.client.infomentor.time.sleep", sleeps.append)
    monkeypatch.setattr("infomentor_mcp.client.infomentor.time.monotonic", lambda: 10)
    endpoint = "https://hub.infomentor.se/calendarv2/calendarv2/getentries"
    httpx_mock.add_response(url=endpoint, json=[])
    httpx_mock.add_response(url=endpoint, json=[])

    def news(_):
        for _ in range(2):
            client._http.hub_post("/calendarv2/calendarv2/getentries", {})
        return []

    monkeypatch.setattr(client, "_get_selected_child_news", news)
    client.collect_changes(["news"], ["one"], date(2026, 1, 1), date(2026, 1, 2),
                           request_delay_seconds=0.5)
    assert sleeps == [0.5]


def test_mcp_change_delivery_does_not_need_a_live_session(tmp_path):
    changes = ChangeStore(tmp_path / "changes.sqlite3")
    changes.record(batch())
    changes.record(batch("Updated"))
    server = create_server(SessionStore(tmp_path / "session.json"), change_store=changes)
    result = asyncio.run(server.call_tool("get_changes", {"consumer": "agent"})).structured_content
    assert result["has_changes"]
    ack = asyncio.run(server.call_tool("ack_changes", {
        "consumer": "agent", "cursor": result["next_cursor"],
    })).structured_content
    assert ack["status"] == "ok"
    assert "synthetic-binding" not in json.dumps(result)


def test_sync_returns_old_pending_work_on_unchanged_fetch(collecting_client, monkeypatch, tmp_path):
    client, _ = collecting_client
    store = ChangeStore(tmp_path / "changes.sqlite3")
    store.record(batch())
    store.record(batch("Updated"))
    monkeypatch.setattr(client, "collect_changes", lambda *_: batch("Updated"))
    result = sync(client, store, consumer="agent")
    assert result["new_changes"] == 0 and result["has_changes"]
    store.ack("agent", result["next_cursor"])
    assert not sync(client, store, consumer="agent")["has_changes"]


def test_cli_sync_get_ack_and_reset_are_json(monkeypatch, capsys):
    from infomentor_mcp import cli

    session = SessionStore()
    session.save(AuthSession(mobile_binding="synthetic-binding"))
    current = batch()
    monkeypatch.setattr(InfoMentorClient, "collect_changes", lambda *_, **__: current)
    assert cli.main(["sync", "--consumer", "agent"]) == 0
    assert not json.loads(capsys.readouterr().out)["has_changes"]
    current = batch("Changed")
    assert cli.main(["sync", "--sources", "news", "--consumer", "agent"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["has_changes"] and result["new_changes"] == 1
    assert cli.main(["changes", "get", "--consumer", "agent"]) == 0
    assert json.loads(capsys.readouterr().out)["changes"] == result["changes"]
    assert cli.main(["changes", "ack", "--consumer", "agent",
                     "--cursor", result["next_cursor"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert cli.main(["changes", "reset", "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "reset"


def test_mcp_sync_defaults_and_partial_result(collecting_client, monkeypatch, tmp_path):
    client, _ = collecting_client
    session = SessionStore(tmp_path / "session.json")
    session.save(AuthSession(mobile_binding="synthetic-binding"))
    received = []

    def collect(*args):
        received.append(args)
        result = batch()
        result["errors"] = [{"source": "calendar", "child_id": "one", "code": "fetch_failed"}]
        return result

    monkeypatch.setattr(client, "collect_changes", collect)
    server = create_server(session, lambda _: client)
    result = asyncio.run(server.call_tool("sync_changes", {})).structured_content
    assert result["status"] == "partial"
    assert received[0][0] == ["news", "calendar"]
    assert received[0][-2:] == (True, 1.0)


def test_invalid_sources_delay_and_dates_make_no_requests(collecting_client):
    client, log = collecting_client
    for sources, start, end, delay in [
        ([], date(2026, 1, 1), date(2026, 1, 2), 1),
        (["notifications"], date(2026, 1, 1), date(2026, 1, 2), 1),
        (["news"], date(2026, 1, 2), date(2026, 1, 1), 1),
        (["news"], date(2026, 1, 1), date(2026, 1, 2), float("nan")),
    ]:
        with pytest.raises(ValueError):
            client.collect_changes(sources, None, start, end, request_delay_seconds=delay)
    assert not log


def test_binding_mismatch_is_rejected_before_network(collecting_client, tmp_path):
    client, log = collecting_client
    store = ChangeStore(tmp_path / "changes.sqlite3")
    original = batch()
    original["binding"] = "different-authorization"
    store.record(original)
    with pytest.raises(ChangeTrackingError) as error:
        sync(client, store)
    assert error.value.code == "tracking_reset_required"
    assert not log


def test_http_collection_and_disabled_includes(collecting_client, monkeypatch, httpx_mock):
    client, log = collecting_client
    monkeypatch.setattr(client, "_get_selected_child_news",
                        InfoMentorClient._get_selected_child_news.__get__(client))
    monkeypatch.setattr(client, "_get_selected_child_calendar",
                        InfoMentorClient._get_selected_child_calendar.__get__(client))
    monkeypatch.setattr(client, "_enrich_news", lambda *_: pytest.fail("Attachments disabled"))
    for _ in range(2):
        httpx_mock.add_response(
            url="https://hub.infomentor.se/Communication/News/GetNewsList",
            json={"items": [{"id": "post", "title": "News", "content": "Synthetic"}]},
        )
        httpx_mock.add_response(
            url="https://hub.infomentor.se/calendarv2/calendarv2/getentries", json=[],
        )
    result = client.collect_changes(["news", "calendar"], None,
                                    date(2026, 1, 1), date(2026, 1, 2), False, 0)
    assert not result["errors"] and len(result["snapshots"]) == 4
    assert result["snapshots"][0]["items"][0]["title"] == "News"
    assert log == ["discover", "select:one", "select:two", "select:one"]


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
