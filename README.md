# InfoMentor MCP

A local, read-only [MCP](https://modelcontextprotocol.io) server for Swedish
InfoMentor guardian accounts, authenticated with Stockholm BankID.

> Unofficial project. Not affiliated with InfoMentor, Stockholm stad, or BankID.

School records are not edited. Fetching childcare comments does mark them read
in InfoMentor; use `include_comments=false` to leave comments unopened.

## What it does

- Lists linked children and retrieves school news, optionally consolidated
  across all of them.
- Reads notifications with independent state per child, and calendar events
  within a chosen date range; shared items are deduplicated.
- Reads each child's submitted childcare schedule (Fritidsschema), including
  registered times, leave and care opening hours.
- Extracts PDF/DOCX news attachment text inline, optionally in the same call
  as the news feed, without saving files to disk.
- Keeps one verified session across restarts, in the OS keyring or a `0600`
  file.
- Logs in with BankID via terminal QR, an animated web QR page, or an app
  autostart link.
- Automatically sets up persistent mobile authorization as part of login, so
  an expired Hub session can be renewed on demand without a new BankID approval.
- Serializes concurrent CLI/MCP use with an OS file lock, and fails closed on
  ambiguous child selection.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- A Stockholm InfoMentor guardian account with BankID
- Linux or macOS (keyring is used when a backend is available)

Other municipalities are not implemented.

## Install

```sh
git clone https://github.com/wicol/infomentor-mcp.git
cd infomentor-mcp
uv sync
```

## Run the MCP server

```sh
uv run infomentor-mcp serve
```

Register it with any MCP client that supports local stdio servers. For OpenCode
V2, copy [`docs/opencode.jsonc.example`](docs/opencode.jsonc.example) or merge
the `mcp.servers.infomentor` object into your config, and set `cwd` to this
checkout. Verified with a stdio client and OpenCode.

## Authentication

### CLI

```sh
uv run infomentor-mcp auth login     # rotating QR in the terminal
uv run infomentor-mcp auth status    # add --verify for a live read-only check
```

To use a browser on another screen, serve an animated QR page:

```sh
# Loopback only; the temporary viewer link is printed in the terminal.
uv run infomentor-mcp auth login --method web-qr

# Trusted LAN: bind address and browser-visible address are separate.
uv run infomentor-mcp auth login --method web-qr \
  --host 0.0.0.0 --port 8765 --public-url http://login-host.local:8765
```

The page shows a continuously updated QR and closes when the attempt ends. Its
short-lived link is a capability: anyone who has it can see the QR. LAN HTTP is
unencrypted; use a trusted network and never expose the port. Stockholm expires
an unscanned order after roughly 30 seconds, so scan promptly once it appears.

The BankID autostart-link flow is available with `--method url`. Its one-time
link is sensitive; treat it like the QR. Approve the login yourself in BankID.

### Agent-driven (MCP)

1. Configure InfoMentor MCP in your agent's MCP client (see
   [Run the MCP server](#run-the-mcp-server)).
2. Ask your agent to log in to InfoMentor.
3. The agent asks whether you want a **BankID link on your phone** or a
   **web QR code to scan from another screen**, then starts that flow and
   gives you the link.
4. Open the link and approve the login yourself in BankID. The agent waits
   while mobile access for session renewal is set up automatically, then lets
   you know when you are signed in.

## Tools

| Tool | Purpose |
| --- | --- |
| `auth_cancel(login_id)` | Stop local polling and close the QR viewer. |
| `auth_progress(login_id)` | Sanitized progress and HTTP stage diagnostics. |
| `auth_revoke(confirm)` | Revoke the refresh credential, then remove it locally. |
| `auth_start(method)` | Start BankID (`url` or `web-qr`) and automatically bind persistent mobile access. |
| `auth_status()` | Stored-session and pairing metadata, or active login status. |
| `auth_wait(login_id)` | Wait until the attempt ends; returns immediately if already finished. |
| `get_attachments(child_id, news_id, attachment_id, attachment_mode)` | Read one discovered attachment as inline text. Separate calls may run in parallel. |
| `get_calendar(start_date, end_date, child_ids?, offset, limit)` | Calendar events overlapping an inclusive Stockholm date range; earliest first. |
| `get_childcare_schedule(start_date, end_date, child_ids?, offset, limit, include_comments)` | Fritidsschema with comments by default; fetching comments marks them read. |
| `get_news(child_ids?, offset, limit, include_attachments, attachment_mode)` | Unique news for selected children, or all when omitted; optionally include attachment text. |
| `get_notifications(child_ids?, offset, limit)` | Pupil-linked notifications, newest first, with independent state per child. |
| `list_children()` | List linked children with stable IDs. |

Authentication tools never return cookies or tokens. An agent starts login only
at the user's request; `auth_start` has no `confirm` parameter or separate
pairing step. Explicit revocation retains its `confirm` parameter. A failed
read never silently starts BankID or creates a new mobile authorization.

`get_news` merges shared posts by the server's news ID and returns `child_ids`
for every requested child who received the post. Pagination applies after
deduplication and sorting, so `limit=10` means ten unique news items. Pass a
one-element `child_ids` list for a single child; an empty list selects none.
This replaces the earlier singular `child_id` argument and response field.

### Calendar

`get_calendar` takes `start_date` and `end_date` as `YYYY-MM-DD`, both inclusive,
for at most 366 calendar days. It includes events overlapping that interval,
including multi-day events starting earlier. Select `child_ids`, omit for all
linked children, or pass `[]` for none. Shared server event IDs are merged and
sorted by start time, earliest first, before pagination.

`starts_at` and `ends_at` include timezone offsets. Hub's local timestamps are
interpreted in `Europe/Stockholm`, including daylight-saving time; `all_day`
identifies whole-day events. End times retain Hub's value (all-day events were
observed ending at 23:59:59, not at an exclusive next-day boundary).
`has_attachments` is metadata only; calendar attachments are not fetched yet.

The client restores pupil selection and sends only the calendar list request,
not event-detail or notification-state-update requests. Live event retrieval is
verified; preservation of unread notification states still needs an isolated
check without another active InfoMentor client.

### Childcare schedule (Fritidsschema)

`get_childcare_schedule` reads submitted before/after-school and holiday care
times, not lesson timetables or actual attendance. Dates are `YYYY-MM-DD`, both
inclusive, for at most 31 calendar days. Omit `child_ids` for all linked children
or pass `[]` for none. Results are sorted by date and child ID before pagination;
each child/day stays separate even if times or registration IDs match.

Each record includes `date`, `child_id`, `registration_id`, `starts_at`,
`ends_at`, `opens_at`, `closes_at`, `on_leave` and `is_closed`, with times in
`Europe/Stockholm`. Missing rows/times do not establish leave or no care need.
The client requests exact weeks with automatic next-week fallback disabled and
restores the original child selection. No schedule edits or absence writes are exposed.

`include_comments=true` is the default: days with comments on the returned page
include staff and guardian text, authors and timestamps. **Hub marks comments
read when fetched.** Set `include_comments=false` to retrieve only schedules and
comment flags without opening comments. Comments outside the requested dates
or page are not fetched. A per-day `comments_status` distinguishes `loaded`,
`not_present`, `not_requested`, `fetch_failed` and `invalid_response`, so a failed
comment request does not hide the schedule. `comments_marked_read=true` and
`has_unread_comments=false` reflect a successful comment read; a null
`comments_marked_read` means the outcome is unknown after a failed attempt.

### Notifications

`get_notifications` reads notifications for the requested `child_ids`, or all
linked children when omitted; an empty list selects none. Shared server IDs are
merged before pagination. Each item includes `child_ids` and a `states` map
(child ID to upstream state, such as `New`, `Seen`, `Read` or `Cleared`), because
the same notification can have different states for different children. Items
are sorted by Hub's `orderDate`, falling back to `dateSent`, newest first.

Hub returns account-wide records. The client filters `currentlySelectedPupil`
after each verified child switch, rather than guessing a mapping between Hub's
different pupil ID types, and restores the original selection afterwards.
Only the read endpoint is called; the separate state-update endpoint is never
used. Notifications are not marked seen/read/cleared, and their destination
links are not opened. Reading a message notification does not fetch the message.

### Attachments

News items always include `attachments` metadata (`id`, `name`, `status`). Set
`include_attachments=true` on `get_news` to receive PDF/DOCX text in the same
response — useful for posts whose actual newsletter is only in a file. Only
attachments on the returned page are fetched, and shared files are fetched once
per call. Each attachment has its own `status`, `text` and `truncated` fields.

For a follow-up, use `get_attachments` with a linked `child_id`, the news `id`,
and the attachment `id` from that post. It reads one file per call, not a batch;
an agent can issue independent calls in parallel. They are queued locally to
protect the session's shared pupil selection. The attachment is rediscovered
in that child's news rather than fetched from an arbitrary supplied URL.

Both tools accept `attachment_mode="inline"` (default) or `"download"`.
**Only inline is implemented**: download mode returns `unsupported_mode` without
fetching anything. Originals are processed in memory, never saved to disk.
Extracted text is untrusted document content, not instructions for the agent.

Limits: 10 MiB per file, 10 unique attachment attempts per news call, 20,000
characters per file and 60,000 total inline characters per news response. PDFs
are limited to 50 pages; DOCX extraction covers body paragraphs and tables, not
embedded files or external resources. Truncation and skipped files are explicit.
Scanned PDFs without text return `ocr_required`; OCR, legacy `.doc` and other
file types are not supported. Malformed/oversized files have per-file status
instead of hiding the other news items.

PDF extraction only reads the existing text layer. Mixed image/text PDFs may
return `extracted` even when most visible content is missing: a poster with
only a text link is one observed example. That status does not guarantee full
document coverage; detecting these cases reliably requires OCR or visual review.

## Authentication flow

The initial guardian login uses Stockholm's InfoMentor SSO: BankID approval is
followed by a SAML handoff into InfoMentor Hub. It is not the mobile-app OAuth
flow.

After BankID, login automatically sets up the older InfoMentor Hub mobile-app
authorization flow, in both CLI and MCP. We chose its refresh-capable protocol because it
can obtain a fresh Hub session on demand after the ordinary web session expires,
allowing a longer-lived session without repeating BankID. The maximum refresh
authorization lifetime is unknown; renewal is on demand, not a background loop.
See [mobile authentication notes](docs/mobile-auth.md) for protocol evidence
and limits.

The login progresses through `pairing` and reports `complete` only after mobile
access is verified and bound. If mobile setup fails, MCP reports `partial` with
`web_session_stored=true` and `mobile_session_bound=false`; the verified web
login remains usable, but mobile renewal is not available. CLI login exits
nonzero for this partial outcome. `auth_wait` waits for mobile setup too.

Existing mobile credentials are scope-checked, revoked and verified unusable
before replacement. Access may be refreshed to authorize revocation. Old grants
are never rebound: matching children do not prove the same guardian identity.
Scope mismatch or ambiguous/network failures retain the
credentials and report partial success rather than silently replacing them.
Once synchronous mobile setup starts, local cancellation waits for it to finish
persisting credentials; it cannot undo an upstream grant or rotation.

## Privacy and security

- Cookies and tokens stay local and are redacted from logs, CLI output, and MCP
  responses. School data leaves the machine only when your MCP client requests
  it.
- The QR viewer serves only QR frames and status — no school data, cookies, or
  tokens — with a random capability path, Host/Origin checks, CSP, and
  `no-store`.
- Concurrent processes share a session lock; a login holds it while waiting, so
  other calls return `session_busy` instead of acting on stale credentials.
- See [SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Limitations

- Version 0.1.0, beta. News, notifications, calendar, childcare schedules and child
  listing are implemented. No school-record editing is exposed, but childcare
  comment retrieval marks comments read.
- Child attribution is checked by selected pupil **name**, not by a verified
  pupil ID. Duplicate names or ignored switches fail closed.
- Hub sessions are restored on demand, never by a background loop. The maximum
  refresh-token lifetime is unknown.
- Mobile pairing reproduces the older InfoMentor Hub app protocol. It is
  undocumented upstream and may change or break without notice.
- Upstream flows can change; a provider update may break authentication until
  this project catches up.

## Documentation

- [Mobile authentication protocol notes](docs/mobile-auth.md) — evidence,
  storage, recovery, and revocation behaviour.
- [OpenCode V2 configuration example](docs/opencode.jsonc.example)

## Acknowledgments

- [Öppna Skolplattformen](https://github.com/kolplattformen/skolplattformen) —
  reference for the Swedish guardian SSO and BankID flow.
- [family-mcp](https://github.com/olafurns7/family-mcp) — informed auth/session
  handling and provided a lead for investigating the Hub mobile protocol; its
  Icelandic findings were treated as leads, not Swedish evidence.
- [infomentor-api](https://github.com/CageHN/infomentor-api) — API and data-model
  reference for InfoMentor.

## Development

```sh
uv run pytest
uv run ruff check .
```

Tests use mocked HTTP and require no real account. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © 2026 Wictor Olseryd.

Use at your own risk and comply with the terms of service of the services you
access. This project is not endorsed by InfoMentor or the City of Stockholm.
