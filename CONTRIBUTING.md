# Contributing

Thanks for your interest. This project is an unofficial, local-only client for
Swedish InfoMentor guardian accounts.

## Development

```sh
git clone https://github.com/wicol/infomentor-mcp.git
cd infomentor-mcp
uv sync
uv run pytest
uv run ruff check .
```

Tests use mocked HTTP and never require a real account. If you add a new
upstream request, cover both the success shape and a failure/no-auth shape.

## Ground rules

- **Read-only school data.** Do not add operations that change school records,
  send messages, or register devices without prior discussion.
- **No credentials in the repository.** Never commit session files, cookies,
  tokens, QR payloads, or BankID links, including in tests, fixtures, and
  screenshots.
- **No real personal data.** Anonymize names, schools, class lists, and any
  copied news content. Use synthetic fixtures.
- **Explicit intent.** Login is user-initiated and includes persistent mobile
  setup; there is no separate pairing prompt or `auth_start` confirmation flag.
  Never start BankID or create a new grant because a read failed. Explicit
  revocation retains its confirmation gate.
- **Small, focused changes.** Keep the existing layering: protocol, persistence,
  presentation, and MCP tools separate.

## Style

- Python 3.12+, `ruff check .` must pass, line length 100.
- Prefer clear code over clever abstractions; avoid one-line indirection.
- Keep tests maintainable: a few representative cases over exhaustive coverage.

## Pull requests

Describe what changed, why, and how you verified it. State clearly if a change
was not tested against a real account. Never attach real session material.
