import httpx
import pytest

from infomentor_mcp.auth.measurement import MeasurementLog, SessionLifetimeProbe
from infomentor_mcp.errors import AuthenticationExpiredError, InfoMentorError
from infomentor_mcp.storage.session import AuthSession


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def read(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeHttp:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.client = httpx.Client()
        self.client.cookies.set("IMHome", "synthetic-secret", domain="hub.infomentor.se")
        self.closed = False

    def verify_authenticated_read(self):
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return 200

    def session(self):
        return AuthSession.from_client(self.client)

    def reset(self, session):
        session.apply_to(self.client)

    def close(self):
        self.closed = True
        self.client.close()


class Store:
    def __init__(self):
        self.saved = []

    def save(self, session):
        self.saved.append(session)


class SsoLogin:
    def __init__(self, http, *, emit_diagnostics):
        assert emit_diagnostics is False

    def resume_stockholm_sso(self):
        return True


def test_active_probe_records_expiry_bounds_and_renews_once():
    clock = Clock()
    records = []
    http = FakeHttp([None, AuthenticationExpiredError("secret must not be logged"), None])
    renewal = FakeHttp([None])
    store = Store()
    try:
        result = SessionLifetimeProbe(
            http, store, records.append, interval=120, duration=360,
            clock=clock.read, sleep=clock.sleep,
            http_factory=lambda session: renewal, login_factory=SsoLogin,
        ).run()
        assert result == "duration_reached"
        expiry = next(item for item in records if item["event"] == "hub_authentication_failed")
        assert expiry["hub_lifetime_lower_seconds"] == 120
        assert expiry["hub_lifetime_upper_seconds"] == 240
        assert records[-1]["successful_renewals"] == 1
        assert renewal.closed
        assert len(store.saved) == 3
        assert "secret" not in str(records)
    finally:
        http.close()
        renewal.close()


def test_idle_probe_waits_without_keepalive_requests():
    clock = Clock()
    records = []
    http = FakeHttp([None])
    try:
        result = SessionLifetimeProbe(
            http, Store(), records.append, idle=3600, duration=7200,
            clock=clock.read, sleep=clock.sleep,
        ).run()
        assert result == "idle_check_complete"
        assert clock.sleeps == [3600]
        assert [item["event"] for item in records] == [
            "started", "hub_read_ok", "idle_check_complete"
        ]
    finally:
        http.close()


def test_bankid_required_stops_without_starting_new_order():
    class ExpiredSso(SsoLogin):
        def resume_stockholm_sso(self):
            return False

    clock = Clock()
    http = FakeHttp([AuthenticationExpiredError("expired")])
    renewal = FakeHttp([])
    records = []
    try:
        result = SessionLifetimeProbe(
            http, Store(), records.append, clock=clock.read, sleep=clock.sleep,
            http_factory=lambda session: renewal, login_factory=ExpiredSso,
        ).run()
        assert result == "bankid_required"
        assert records[-1]["event"] == "bankid_required"
        assert renewal.closed
    finally:
        http.close()
        renewal.close()


@pytest.mark.parametrize("error", [
    InfoMentorError("private provider response"),
    httpx.ConnectError("private URL with tokens"),
    httpx.HTTPStatusError(
        "private response", request=httpx.Request("POST", "https://hub.infomentor.se/"),
        response=httpx.Response(429),
    ),
])
def test_network_or_provider_errors_are_inconclusive_and_redacted(error):
    clock = Clock()
    http = FakeHttp([error])
    records = []
    try:
        result = SessionLifetimeProbe(
            http, Store(), records.append, clock=clock.read, sleep=clock.sleep,
        ).run()
        assert result == "inconclusive"
        assert records[-1]["event"] == "inconclusive"
        assert "private" not in str(records)
        assert not any(item["event"] == "sso_attempt" for item in records)
    finally:
        http.close()


def test_metadata_log_is_private_and_never_overwrites(tmp_path, capsys):
    path = tmp_path / "lifetime.jsonl"
    log = MeasurementLog(path)
    try:
        log.emit({"event": "started", "elapsed_seconds": 0})
    finally:
        log.close()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        MeasurementLog(path)
    assert "started" in capsys.readouterr().out
