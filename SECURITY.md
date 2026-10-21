# Security

## Reporting a vulnerability

Report security issues privately through GitHub's **Report a vulnerability**
feature on the repository's Security tab. Do not open a public issue for
anything involving credentials, session handling, or authentication bypasses.

Please include the affected version, the command or tool used, and a sanitized
description of the behaviour. Never include:

- BankID autostart links or QR payloads
- cookies, session files, access or refresh tokens
- school records, names, or other personal data
- raw HTTP bodies or URLs containing query strings

Expect an acknowledgement within a few days. This is a volunteer project and
no bounty program exists.

## Credential handling

- Sessions are stored in the OS keyring when available, otherwise in a `0600`
  file below `$XDG_STATE_HOME`.
- Mobile credentials use a separate keyring entry or a separate `0600` file.
- Tokens and cookies are redacted from CLI output, logs, and MCP responses.
- The web-QR viewer serves only QR frames and status; it does not expose school
  data, cookies, or tokens.
- Non-loopback QR binding requires an explicit public origin and is unencrypted
  HTTP. Only use it on a trusted network, and never expose it to the internet.

## Scope

In scope: local credential storage, redaction, session locking, the temporary
QR web server, and MCP tool authorization boundaries.

Out of scope: the InfoMentor services themselves, BankID, and issues that
require a compromised local account or physical access to an unlocked machine.
