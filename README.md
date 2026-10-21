# InfoMentor MCP

A local, read-only [MCP](https://modelcontextprotocol.io) server for Swedish
InfoMentor guardian accounts, authenticated with Stockholm BankID.

**The point:** one request returns news from every linked child, merged into a
single list instead of switching accounts and scrolling per child. Everything
runs on your machine — there is no hosted intermediary, no account sharing, and
no write access to school data.

> Unofficial project. Not affiliated with InfoMentor, Stockholm stad, or BankID.

## What it does

- Lists linked children and retrieves school news, optionally consolidated
  across all of them.
- Keeps one verified session across restarts, in the OS keyring or a `0600`
  file.
- Logs in with BankID via terminal QR, an animated web QR page, or an app
  autostart link.
- Optionally pairs a long-lived mobile authorization, so an expired Hub session
  can be renewed on demand without a new BankID approval.
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

## Authenticate

```sh
uv run infomentor-mcp auth login     # rotating QR in the terminal
uv run infomentor-mcp auth status    # add --verify for a live read-only check
```

Prefer a browser screen? Serve the QR over HTTP:

```sh
# Loopback only, link is printed in the terminal:
uv run infomentor-mcp auth login --method web-qr

# Trusted LAN: the bind address and the address you actually open are separate.
uv run infomentor-mcp auth login --method web-qr \
  --host 0.0.0.0 --port 8765 --public-url http://login-host.local:8765
```

The web page shows a continuously updated QR and closes when the attempt ends.
The link is a short-lived capability: anyone who has it can see the QR. LAN
HTTP is unencrypted, so use a trusted network and never expose the port.
Stockholm expires an unscanned order after roughly 30 seconds, so scan promptly
once the code appears.

The BankID autostart link (`--method url`) is also available. It contains a
one-time secret; treat it like the QR.

## Run the MCP server

```sh
uv run infomentor-mcp serve
```

Register it with any MCP client that supports local stdio servers. For OpenCode
V2, copy [`docs/opencode.jsonc.example`](docs/opencode.jsonc.example) or merge
the `mcp.servers.infomentor` object into your config, and set `cwd` to this
checkout. Verified with a stdio client and OpenCode.

To make the web QR reachable from another device while running under an MCP
host:

```sh
uv run infomentor-mcp serve --auth-host 0.0.0.0 --auth-port 8765 \
  --auth-public-url http://login-host.local:8765
```

## Tools

| Tool | Purpose |
| --- | --- |
| `list_children()` | List linked children with stable IDs. |
| `get_news(child_id?, offset, limit)` | News for one child, or all children when `child_id` is omitted. |
| `auth_status()` | Stored-session and pairing metadata, or active login status. |
| `auth_start(confirm, method)` | Start BankID (`url` or `web-qr`); requires explicit confirmation. |
| `auth_progress(login_id)` | Sanitized progress and HTTP stage diagnostics. |
| `auth_wait(login_id)` | Block until the attempt ends; returns immediately if already finished. |
| `auth_cancel(login_id)` | Stop local polling and close the QR viewer. |
| `auth_pair(confirm)` | Persist a long-lived mobile authorization after BankID. |
| `auth_revoke(confirm)` | Revoke the refresh credential, then remove it locally. |

Authentication tools never return cookies or tokens, and `confirm` must be
`true` after the user explicitly asks for the action. A failed read does not
silently start a login.

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

- Version 0.1.0, beta. Only news and child listing are implemented; school-data
  operations stay read-only by design.
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
