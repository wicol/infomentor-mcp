from time import time
from unittest.mock import patch

import httpx
import pytest

from infomentor_mcp.errors import AuthenticationExpiredError, InfoMentorError, SessionStorageError
from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.storage.session import AuthSession, SessionStore, StoredCookie


class Renewal:
    def __init__(self, effect=None):
        self.calls = 0
        self.effect = effect

    def renew(self, current):
        self.calls += 1
        if self.effect:
            self.effect()
        return AuthSession()


def expiring(calls, failures=1):
    def operation():
        calls.append("read")
        if len(calls) <= failures:
            raise AuthenticationExpiredError("synthetic expiry")
        return "retried successfully"
    return operation


def test_retries_a_read_once_after_session_renewal(store):
    calls, renewal = [], Renewal()
    with Hub(store, renewal) as hub:
        assert hub.run(expiring(calls)) == "retried successfully"
    assert calls == ["read", "read"] and renewal.calls == 1


def test_expired_session_without_renewal_requires_login(store):
    with Hub(store) as hub, pytest.raises(AuthenticationExpiredError, match="run auth login"):
        hub.run(expiring([]))


def test_replayed_read_is_not_renewed_again(store):
    calls, renewal = [], Renewal()
    with Hub(store, renewal) as hub, pytest.raises(AuthenticationExpiredError):
        hub.run(expiring(calls, failures=2))
    assert calls == ["read", "read"] and renewal.calls == 1


def test_missing_session_requires_login():
    with Hub(SessionStore()) as hub, pytest.raises(AuthenticationExpiredError, match="auth login"):
        hub.run(lambda: pytest.fail("No operation without a session"))


def test_read_reloads_latest_cookies_under_lock_and_persists_rotation(store):
    http = httpx.Client()
    http.cookies.set("IMHome", "old", domain="hub.infomentor.se")
    store.save(AuthSession.from_client(http))
    with Hub(store) as hub:
        hub.run(lambda: None)
        stale = hub.transport.client
        http.cookies.set("IMHome", "latest", domain="hub.infomentor.se")
        store.save(AuthSession.from_client(http))

        def read_and_rotate():
            value = hub.transport.client.cookies.get("IMHome")
            hub.transport.client.cookies.set("IMHome", "rotated", domain="hub.infomentor.se")
            return value

        assert hub.run(read_and_rotate) == "latest"
        assert stale.is_closed  # Another process may have renewed on a different backend.
    assert store.load().cookies[0].value == "rotated"
    http.close()


def test_read_reloads_changed_cookie_attributes_even_when_value_is_unchanged(store):
    session = AuthSession(cookies=[StoredCookie(
        name="synthetic", value="same-value", domain="example.invalid", secure=False,
    )])
    store.save(session)
    with Hub(store) as hub:
        hub.run(lambda: None)
        session.cookies[0].secure = True
        session.cookies[0].expires = int(time()) + 3600
        store.save(session)
        cookie = hub.run(lambda: next(iter(hub.transport.client.cookies.jar)))
    assert cookie.secure is True and cookie.expires == session.cookies[0].expires


def test_reads_still_report_session_save_failure(store):
    with Hub(store) as hub, patch.object(store, "save", side_effect=OSError("synthetic")):
        with pytest.raises(SessionStorageError) as error:
            hub.run(lambda: "synthetic read result")
    assert "synthetic" not in str(error.value)


def test_corrupt_session_is_a_storage_error_without_echoing_it(store):
    with Hub(store) as hub, patch.object(store, "load", side_effect=ValueError("secret-cookie")):
        with pytest.raises(SessionStorageError) as error:
            hub.run(lambda: None)
    assert "secret" not in str(error.value)


def test_successfully_saved_session_is_not_overwritten_when_renewal_later_fails(store):
    def save_newer_then_fail():
        store.save(AuthSession(mobile_binding="newer-session"))
        raise InfoMentorError("synthetic late failure")

    with Hub(store, Renewal(save_newer_then_fail)) as hub, pytest.raises(InfoMentorError):
        hub.run(expiring([]))
    assert store.load().mobile_binding == "newer-session"
