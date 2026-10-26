# Change tracking

Use an external scheduler to fetch changes without an LLM. The first version
supports `news` and `calendar`; omitted `sources` selects both. Notifications,
lesson timetables and childcare are not yet included. Sources are independent:
there is no semantic correlation between channels.

## CLI and MCP

```sh
infomentor-mcp sync --consumer notifier
infomentor-mcp sync --sources news --no-include-attachments --consumer notifier
infomentor-mcp sync --sources calendar --start-date 2026-11-01 --end-date 2026-11-30
infomentor-mcp changes get --consumer notifier --limit 100
infomentor-mcp changes ack --consumer notifier --cursor '<next_cursor>'
```

MCP exposes `sync_changes`, `get_changes`, and `ack_changes` with the same
semantics. CLI outputs JSON, exits 0 on success and 1 on errors or partial fetches.
An external scheduler can run `sync --consumer notifier` every 30 minutes.
Check its status/errors and `has_changes`; start an agent only for pending work.
Do not log the returned school data to a public cron log.

`sync` fetches, persists, and returns a page of **pending** changes, including
unacknowledged changes from earlier runs. `new_changes` counts newly detected
changes, while `has_changes` describes pending work for this consumer. A failed
agent run therefore does not lose work even if the next fetch is unchanged.
`get` reads the same local queue without contacting InfoMentor.

Process the entire returned page before passing its `next_cursor` to `ack`.
If `has_more` is true, read and process the next page after acknowledging this
one. Reads do not consume work. Consumers have independent acknowledged
positions. Delivery is at least once: use `change_id` to avoid duplicate effects
after a crash. Concurrent workers for the same consumer require external
coordination; there are no worker leases or exactly-once guarantees.

## Fetch behavior

- `child_ids` defaults to all linked children; an empty list selects none.
- Calendar defaults to today through 90 days ahead, inclusive, in
  `Europe/Stockholm`. Explicit ranges may contain at most 366 days.
- News uses the existing Hub news-list endpoint and its returned feed, not an
  unlimited historical export.
- `include_attachments` defaults to true. `--no-include-attachments` disables it.
- `request_delay_seconds` (`--request-delay-seconds`) defaults to 1, accepts
  0–60, and spaces data, page, attachment and pupil-switch requests. Authentication
  renewal itself uses the existing authentication flow, not this pacing hook.
- Children are discovered/verified once, then all selected sources are fetched
  per child, starting with the child left selected by verification so no switch
  is repeated. Original selection is restored, including on failure. The existing
  shared session lock protects the operation. An expired session may renew and
  replay the collection once; no changes are committed from an abandoned attempt.

The first successful fetch for each child/source creates a baseline without
historical alerts. Attachment-enabled and attachment-disabled news have separate
baselines, so switching includes does not look like content deletion. Existing
known IDs continue to be compared when calendar windows move. Newly observed
IDs are reported as `added`, **not** proof of recent upstream creation.
Omitted sources, empty windows and failed fetches never imply deletion. This
version does not report removals, including events moved outside the window.

Complete child/source snapshots and their changes are committed transactionally.
Failures are returned per child/source; other successful sources may advance.
Retrieval timestamps, raw payloads and read/unread status are excluded. Identical
changes to a shared server ID are merged across children within a sync, but never
across source types.

## Attachments

Hub never replaces the file behind an attachment ID, so each file is read once:
a file shared by several children is downloaded once per sync, and a file with a
definitive outcome (extracted, no text, OCR required, encrypted, invalid, too large
or unsupported) is reused from the snapshots of later syncs. Failed and limited
reads are retried on the next sync. Successfully downloaded supported files keep a
SHA-256 of the original bytes. The existing size, count, text and format limits
apply **per child** during sync. `attachment_checks` reports each file's status and
whether a hash of its bytes is known. Calendar attachment content is not fetched
by the existing calendar reader.

A failed/limited read preserves previously known file content in the snapshot;
it is not evidence that the file became empty or remained unchanged. Inspect
`attachment_checks` even when the overall fetch succeeds. Unsupported or
unfetched files have no content-change guarantee. Extraction is not OCR.

## Local state and reset

The SQLite database is `changes.sqlite3` beside the session fallback location
under `$XDG_STATE_HOME/infomentor-mcp` by default. It is owner-readable/writable
only and contains school data, not cookies or tokens. It stores a hash of the
mobile authorization binding, not a reusable credential. There is no proven
stable guardian-account identifier, so an unbound login is refused and a fresh
BankID authorization requires an explicit tracking reset. Normal session/token
renewal retains the binding and does not reset history.

Change history and snapshots not observed for 90 days are pruned on successful
sync. Current snapshots remain while observed. A consumer behind the retained
history gets an explicit expired-cursor error rather than silently skipping work.
Consumers first registered after cleanup begin at the retained history boundary.

```sh
infomentor-mcp changes reset --confirm
```

Reset deletes all snapshots, pending changes and consumer positions, without
logging out or changing school records. The next sync establishes fresh baselines;
old cursors cannot acknowledge the new log. Reset is not a secure-erasure promise
for filesystem copies/backups. Locally stored data can still be read after logout;
reset it explicitly when removing an account or sharing a machine.
