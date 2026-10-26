"""Private snapshots and at-least-once delivery, independent of an agent host."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from infomentor_mcp.errors import ChangeTrackingError, InfoMentorError

RETENTION_SECONDS = 90 * 86400


class ChangeStore:
    """One explicitly bound authorization per DB; a new login requires an explicit reset."""

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def check_consumer(consumer: str, limit: int) -> None:
        if not consumer.strip() or len(consumer) > 100 or not 1 <= limit <= 1000:
            raise ChangeTrackingError(
                "Use a consumer name of 1-100 characters and limit of 1-1000."
            )

    @staticmethod
    def canonical(item: dict, include_attachments: bool) -> dict:
        """Only user-facing content participates; never retain raw school/auth payloads."""
        item = {k: v for k, v in item.items()
                if k not in {"raw", "retrieved_at", "child_ids"}}
        if not include_attachments:
            item.pop("attachments", None)
        elif "attachments" in item:
            item["attachments"] = sorted(
                item["attachments"], key=lambda a: (a["id"] or "", a["name"] or ""),
            )
        return item

    @contextmanager
    def connect(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            os.chmod(self.path, 0o600)
            db = sqlite3.connect(self.path, timeout=30)
        except (OSError, sqlite3.Error):
            raise self._storage_error() from None
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA secure_delete=ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS baselines (scope TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS snapshots (
                    scope TEXT, entity TEXT, payload TEXT, seen INTEGER,
                    PRIMARY KEY(scope, entity));
                CREATE TABLE IF NOT EXISTS changes (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, created INTEGER);
                CREATE TABLE IF NOT EXISTS consumers (
                    name TEXT PRIMARY KEY, ack INTEGER NOT NULL, delivered INTEGER NOT NULL);
            """)
            with db:
                yield db
        except InfoMentorError:
            raise
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            raise self._storage_error() from None
        finally:
            db.close()

    def record(self, batch: dict, include_attachments: bool = True) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._bind(db, batch["binding"])
            now = int(datetime.now().timestamp())
            self._expire(db, now - RETENTION_SECONDS)
            pending = {}
            baselines = []
            for snapshot in batch["snapshots"]:
                if self._record_snapshot(db, snapshot, include_attachments, now, pending):
                    baselines.append(
                        {"child_id": snapshot["child_id"], "source": snapshot["source"]}
                    )
            epoch = db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
            for change in pending.values():
                row = db.execute("INSERT INTO changes(payload, created) VALUES (?, ?)",
                                 (json.dumps(change, ensure_ascii=False), now))
                change["change_id"] = f"{epoch}:{row.lastrowid}"
                db.execute("UPDATE changes SET payload=? WHERE seq=?",
                           (json.dumps(change, ensure_ascii=False), row.lastrowid))
            return {"schema_version": 1, "status": "partial" if batch["errors"] else "ok",
                    "has_changes": bool(pending), "new_changes": len(pending),
                    "baselines_created": baselines, "errors": batch["errors"],
                    "attachment_checks": batch.get("attachment_checks", [])}

    def known_attachments(self) -> list[dict]:
        """Every attachment in the current news snapshots, repeated for shared news."""
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM snapshots").fetchall()
            items = [json.loads(payload) for (payload,) in rows]
        return [attachment for item in items for attachment in item.get("attachments", [])]

    def check_binding(self, binding: str | None) -> None:
        with self.connect() as db:
            self._bind(db, binding, create=False)

    def get(self, consumer: str = "default", limit: int = 100) -> dict:
        self.check_consumer(consumer, limit)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
            if epoch is None:
                return {"schema_version": 1, "changes": [], "has_changes": False,
                        "next_cursor": None, "has_more": False}
            floor = db.execute("SELECT value FROM meta WHERE key='floor'").fetchone()
            floor = int(floor[0]) if floor else 0
            db.execute("INSERT OR IGNORE INTO consumers VALUES (?, ?, ?)", (consumer, floor, floor))
            state = db.execute("SELECT ack FROM consumers WHERE name=?", (consumer,)).fetchone()[0]
            if state < floor:
                raise ChangeTrackingError(
                    "Consumer cursor expired after retention; reset tracking explicitly.",
                    code="cursor_expired",
                )
            rows = db.execute("SELECT seq, payload FROM changes WHERE seq>? ORDER BY seq LIMIT ?",
                              (state, limit + 1)).fetchall()
            page = rows[:limit]
            cursor = page[-1]["seq"] if page else state
            db.execute("UPDATE consumers SET delivered=MAX(delivered, ?) WHERE name=?",
                       (cursor, consumer))
            return {"schema_version": 1, "changes": [json.loads(r["payload"]) for r in page],
                    "has_changes": bool(page), "next_cursor": f"{epoch[0]}:{cursor}",
                    "has_more": len(rows) > limit}

    def ack(self, consumer: str, cursor: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
            state = db.execute("SELECT ack, delivered FROM consumers WHERE name=?",
                               (consumer,)).fetchone()
            try:
                prefix, sequence = cursor.split(":")
                sequence = int(sequence)
            except (ValueError, AttributeError):
                raise ChangeTrackingError("Invalid cursor.") from None
            if not epoch or prefix != epoch[0] or not state or not state[0] <= sequence <= state[1]:
                raise ChangeTrackingError("Cursor was not delivered to this consumer or is stale.")
            db.execute("UPDATE consumers SET ack=? WHERE name=?", (sequence, consumer))
            return {"status": "ok", "consumer": consumer, "cursor": cursor}

    def reset(self) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in ("meta", "baselines", "snapshots", "changes", "consumers"):
                db.execute(f"DELETE FROM {table}")
        return {"status": "reset"}

    @staticmethod
    def _storage_error() -> ChangeTrackingError:
        return ChangeTrackingError(
            "Cannot read or save private change tracking state.", code="change_storage_error",
        )

    @staticmethod
    def _bind(db, binding: str | None, *, create: bool = True) -> None:
        if not binding:
            raise ChangeTrackingError("Change tracking requires a bound mobile login.",
                                      code="authentication_required")
        fingerprint = hashlib.sha256(binding.encode()).hexdigest()
        existing = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
        if existing and existing[0] != fingerprint:
            raise ChangeTrackingError(
                "Authorization changed; explicitly reset change tracking first.",
                code="tracking_reset_required",
            )
        if create:
            db.execute("INSERT OR IGNORE INTO meta VALUES ('binding', ?)", (fingerprint,))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('epoch', ?)", (uuid4().hex,))

    @staticmethod
    def _expire(db, cutoff: int) -> None:
        """Drop changes and snapshots past retention; slower consumers then get cursor_expired."""
        expired = db.execute(
            "SELECT MAX(seq) FROM changes WHERE created < ?", (cutoff,),
        ).fetchone()[0]
        if expired is not None:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('floor', ?)", (str(expired),))
            db.execute("DELETE FROM changes WHERE seq <= ?", (expired,))
        db.execute("DELETE FROM snapshots WHERE seen < ?", (cutoff,))

    def _record_snapshot(self, db, snapshot, include_attachments, now, pending) -> bool:
        """Diff one complete child/source snapshot into `pending`; True if it was a baseline."""
        child, source = snapshot["child_id"], snapshot["source"]
        # Windows deliberately do not define identity; moving a window is not deletion.
        scope = json.dumps([child, source, include_attachments if source == "news" else None])
        baseline = db.execute(
            "SELECT 1 FROM baselines WHERE scope=?", (scope,),
        ).fetchone() is None
        for raw in snapshot["items"]:
            item = self.canonical(raw, include_attachments)
            entity = item["id"]
            previous = db.execute(
                "SELECT payload FROM snapshots WHERE scope=? AND entity=?", (scope, entity),
            ).fetchone()
            before = json.loads(previous[0]) if previous else None
            if include_attachments and source == "news" and before:
                self._keep_known_attachment_text(before, item)
            if not baseline and before != item:
                fields = sorted(k for k in set(before or {}) | set(item)
                                if (before or {}).get(k) != item.get(k))
                change = {"entity_type": source, "entity_id": entity,
                          "operation": "updated" if before else "added",
                          "changed_fields": fields, "before": before, "after": item}
                key = json.dumps(change, sort_keys=True)
                pending.setdefault(key, {**change, "child_ids": []})["child_ids"].append(child)
            db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?)",
                       (scope, entity, json.dumps(item, sort_keys=True, ensure_ascii=False), now))
        db.execute("INSERT OR IGNORE INTO baselines VALUES (?)", (scope,))
        return baseline

    @staticmethod
    def _keep_known_attachment_text(before: dict, item: dict) -> None:
        """Failed, limited or unchanged attachment reads must not erase known content."""
        old_files = {a["id"]: a for a in before.get("attachments", [])}
        for index, attachment in enumerate(item.get("attachments", [])):
            previous_file = old_files.get(attachment["id"])
            if previous_file and (
                not attachment.get("content_sha256")
                or attachment["content_sha256"] == previous_file.get("content_sha256")
            ):
                item["attachments"][index] = {
                    **attachment, **{key: previous_file[key] for key in (
                        "text", "truncated", "content_sha256", "status",
                    ) if key in previous_file},
                }
