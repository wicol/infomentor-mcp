# Experimental Hub mobile authentication

This implementation uses the older **InfoMentor Hub** app protocol. Initial
pairing and revocation are explicit, confirmation-gated CLI or MCP actions.
Existing bound credentials can restore an expired Hub session on demand through
the normal MCP/client read path. It never registers push notifications,
installs an APK, or modifies school records.

## Evidence

The Hub Android 1.0.85 XAPK (`net.infomentor.android`) was downloaded from the
APKPure link published on its download page for static inspection only:

`https://apkpure.net/infomentor-hub/net.infomentor.android/download`

SHA-256:
`9267eae9ae8286ac850701d1f55e4e7906c77133d6786c4fc45302c4fa1d9ad6`.

This matches the family-mcp research artifact but is **not** an independent
publisher-signature verification. No app code was installed or executed.
Androguard was used in a temporary isolated Python environment outside this
project, not added as a runtime or project dependency. No decompiled app source
is distributed with this project; the implementation independently reproduces
the observed wire protocol.

Independent static observations from Retrofit annotations and DEX call sites:

- Swedish web entry: `https://infomentor.se/Swedish/Production/mentor/?isimhapp=1`.
- `GET /Authentication/Authentication/LoginOAuth2` uses `authGuid`,
  `DeviceIdentifier`, `DeviceFriendlyName`, `DevicePlatform`, `scope`,
  `redirect_uri`, `client_id`, `response_type`, and `isSandBox`.
- The app uses `notificationapp`, `IM2-API-NOTIFICATION`, platform `Android`,
  and `InfomentorNotification://oauth2Callback`.
- Form-encoded `POST /Authentication/OAuth2/Token` exchanges `code`,
  `client_id`, `client_secret`, `redirect_uri`, and `grant_type`.
  The app's literal `client_secret=NONE` is a public protocol constant, not
  an extracted private secret.
- Refresh uses the same endpoint with `refresh_token`, `client_id`,
  `client_secret`, `grant_type=refresh_token`, and `scope`.
- `GET /NA1/Authentication/sso` receives a bearer token and returns a web
  sign-in URL. The app refreshes on HTTP 401 and persists both returned tokens.
- Revocation is `GET /Authentication/OAuth2/RevokeRefreshToken` with a bearer
  header and a `refreshToken` query parameter. The query is never logged here.

## Commands

After an ordinary user-approved BankID web login:

```sh
uv run infomentor-mcp auth mobile-discover
uv run infomentor-mcp auth mobile-pair --confirm
```

With an MCP client, the equivalent pairing action is `auth_pair(confirm=true)`
after a successful BankID login. Revocation is `auth_revoke(confirm=true)`,
equivalent to `auth mobile-revoke --confirm` below. Both require explicit user
approval; neither runs automatically when a school-data read fails. The CLI
discovery and test commands below are not separate MCP tools.

Pairing explicitly creates a new app authorization. It generates its **own**
random UUID and an identifying `InfoMentor MCP` device name. It does not copy
the user's Android device ID or app credentials. `DevicePlatform=Android`
matches the inspected app protocol; the actual client runs locally in Python.

The callback is consumed directly from the authorization response, not opened
as an application URL. Only the exact inspected callback is accepted.
Discovery endpoints must match the live-verified Swedish configuration:
`im.infomentor.se` for authorization/token/revoke and `api-im.infomentor.se`
for the bearer-authenticated API. Token-bearing HTTP requests never follow
redirects, and the web sign-in chain never receives a bearer header.

```sh
# Fresh Hub session using the stored access token; no original Hub cookies.
uv run infomentor-mcp auth mobile-test

# Explicit refresh exchange followed by fresh Hub verification.
uv run infomentor-mcp auth mobile-test --refresh

# Wait until advertised access expiry, require HTTP 401, then refresh and verify.
uv run infomentor-mcp auth mobile-test --after-expiry

# Revoke, verify that the old refresh credential is rejected, then clear locally.
uv run infomentor-mcp auth mobile-revoke --confirm
```

`--after-expiry` holds the common session lock while waiting, with no keepalive
requests. Other MCP/auth operations will return busy. A successful immediate
`--refresh` alone does not demonstrate renewal after expiry. No refresh-token
lifetime is inferred from access-token `expires_in`.

## Storage and recovery

- Mobile credentials use a distinct OS-keyring entry, or a separate
  `mobile-credentials.json` beside the web-session file, created atomically as
  `0600`. Tokens are redacted from model representations and CLI output.
- Tokens are persisted immediately after exchange/rotation, **before** SSO or
  further data requests can fail. Failed verification retains them for recovery
  or revocation. Pairing refuses to overwrite an existing credential.
- Fresh Hub sessions must pass a timetable read and match the original linked
  children fingerprint before replacing the saved web session. This is a scope
  check, not proof of a unique guardian identity.
- A random local binding links the mobile credential to its web-session lineage.
  A different explicit web login or logout breaks that binding; mobile tests
  refuse to overwrite such a session. Revocation remains possible independently.
- `auth logout` clears **web cookies only**. Use `mobile-revoke --confirm` to
  invalidate the refresh credential before discarding its local copy. It does
  not guarantee immediate invalidation of existing access tokens or Hub cookies,
  or deletion of an upstream device-registration record.
- Revocation failures or ambiguous responses retain local credentials. A
  repeated revoke can report HTTP 400/401 for an already unusable credential.
  Local removal additionally requires the refresh check to reject the credential
  with a recognized response. The Swedish system was observed returning HTTP
  400 with plain `Bad Request`, not a JSON OAuth error. This establishes the
  tested credential's rejection, not the entire provider's revocation semantics.

## Verification status

Live-tested with the Stockholm guardian account:

- BankID-backed discovery of Swedish mobile endpoints.
- App authorization/code exchange and separate access/refresh-token persistence.
- Token-based sign-in to a completely fresh, read-verified Hub session with the
  same linked-child scope.
- Immediate refresh exchange, persistence, and fresh Hub restoration.
- Refresh-token revocation followed by rejection of the previously working
  refresh credential and removal of its local copy.
- Access-token `expires_in` was 600 seconds in the observed responses.

The background `--after-expiry` run also succeeded live: it waited 597 seconds
without keepalive requests, observed HTTP 401 for the old access token, exchanged
the refresh token, and established a new read-verified Hub session with matching
linked-child scope, without BankID. This verifies renewal after actual access
expiry, not the maximum refresh-token lifetime or multi-day unattended use.
Guarded mobile renewal is now enabled for an already paired web-session
lineage. Hub expiry selects mobile SSO; HTTP 401 from the bearer API selects
one refresh exchange and one SSO retry. No further retry is performed for
rejected new access, or for the replayed data operation. Revocation-in-progress,
missing credentials, or binding mismatch stop without a new grant. Unpaired
sessions retain the earlier cookie-only Stockholm SSO renewal route.

The integration's first live replay attempt failed when new cookies were
installed into the old HTTP transport. Replacing the HTTP client/connection
pool on renewal fixed the replay in the live test, consistent with backend
affinity. The exact load-balancer mechanism was not independently established.
When reloading cookies changed by another process, the client likewise replaces
its transport rather than reusing an old connection pool.

Live integration verification withheld the Cookie header for only the initial
Hub request (HTTP 302), without manually changing saved cookies. Ordinary
`list_children` then restored a fresh Hub session through the existing mobile
pairing and replayed successfully, returning two children. This simulates Hub
cookie loss, not server-side expiry. Actual expired-access-token refresh was
verified separately by the earlier timed HTTP-401 test. A fresh-process web
verification and a real MCP stdio `get_news(limit=1)` call also succeeded.
Synthetic tests cover the combined Hub/access-expiry path, stale-session
overwrite protection, one-replay limits, error redaction, scope/binding checks,
and refusal to refresh on access denials or rate limits.
