"""Command-line interface for InfoMentor MCP."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx

from infomentor_mcp.auth.locking import SessionLockTimeout
from infomentor_mcp.auth.measurement import MeasurementLog, measure_session
from infomentor_mcp.auth.mobile import MobileDiscoveryError, discover_mobile_auth
from infomentor_mcp.auth.mobile_pairing import MobilePairing
from infomentor_mcp.auth.saml import SamlSession
from infomentor_mcp.auth.session import SessionStore
from infomentor_mcp.client.http import (
    AuthenticationExpiredError,
    InfoMentorError,
    InfoMentorHttpClient,
)
from infomentor_mcp.login import terminal_qr, url
from infomentor_mcp.login.attempt import LoginAttempt
from infomentor_mcp.login.complete import complete_mobile_login
from infomentor_mcp.login.web_qr import WebQrOptions
from infomentor_mcp.server import serve_stdio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="infomentor-mcp")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth", help="Manage the authenticated InfoMentor session")
    auth_subparsers = auth.add_subparsers(dest="auth_command", required=True)
    auth_subparsers.add_parser(
        "login", help="Log in with BankID and set up persistent mobile access",
        description="Log in with BankID, then automatically set up mobile session renewal.",
    )
    auth_subparsers.choices["login"].add_argument(
        "--method",
        choices=("qr", "url", "web-qr"),
        default="qr",
        help="Terminal QR (default), BankID app URL, or temporary animated web QR page",
    )
    login = auth_subparsers.choices["login"]
    login.add_argument(
        "--host", default="127.0.0.1", help="Web QR listen IP; 0.0.0.0 for trusted LAN"
    )
    login.add_argument("--port", type=int, default=0, help="Web QR port; 0 chooses a free port")
    login.add_argument(
        "--public-url",
        help="Web QR HTTP origin reachable by your browser, e.g. http://login-host.local:8765",
    )
    auth_subparsers.choices["login"].add_argument(
        "--probe-sso-renewal",
        action="store_true",
        help="Immediately test whether Stockholm SSO can mint a new Hub session without BankID",
    )
    status = auth_subparsers.add_parser("status", help="Show local authentication status")
    status.add_argument(
        "--verify",
        action="store_true",
        help="Restore the stored session in a new process and perform a read-only verification",
    )
    auth_subparsers.add_parser("logout", help="Remove locally stored authentication data")
    auth_subparsers.add_parser(
        "mobile-discover",
        help="Experimental Hub app discovery using saved cookies; no pairing or token exchange",
    )
    pair = auth_subparsers.add_parser(
        "mobile-pair",
        help="Experimental: explicitly authorize a new mobile client and store tokens",
    )
    pair.add_argument("--confirm", action="store_true", required=True)
    test = auth_subparsers.add_parser(
        "mobile-test", help="Test mobile-token SSO into a fresh, verified Hub session"
    )
    test.add_argument("--refresh", action="store_true", help="Explicitly rotate tokens before SSO")
    test.add_argument(
        "--after-expiry", action="store_true",
        help="Wait for access expiry, require HTTP 401, then refresh and verify fresh Hub SSO",
    )
    revoke = auth_subparsers.add_parser(
        "mobile-revoke",
        help="Revoke the experimental refresh token before removing local credentials",
    )
    revoke.add_argument("--confirm", action="store_true", required=True)
    measure = auth_subparsers.add_parser(
        "measure", help="Fresh BankID login followed by automated session-lifetime measurement"
    )
    measure.add_argument("--method", choices=("qr", "url"), default="qr")
    measure.add_argument("--interval-seconds", type=int, default=120)
    measure.add_argument("--duration-minutes", type=int, default=480)
    measure.add_argument(
        "--idle-minutes", type=int,
        help="Leave the fresh session untouched, check once after this delay, then exit",
    )
    measure.add_argument("--log", type=Path, help="New private JSONL log; never overwrites a file")

    serve = subparsers.add_parser("serve", help="Run the MCP server over stdio")
    serve.add_argument("--auth-host", default="127.0.0.1")
    serve.add_argument("--auth-port", type=int, default=0)
    serve.add_argument("--auth-public-url")
    subparsers.add_parser("sync", help="Fetch and record school-data changes")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "auth" and args.auth_command == "login":
        if args.method == "web-qr" and args.probe_sso_renewal:
            build_parser().error("The SSO probe is supported with --method qr or url, not web-qr.")
        return _run_login(args)
    if args.command == "auth" and args.auth_command in {
        "mobile-pair", "mobile-test", "mobile-revoke"
    }:
        return _run_mobile_flow(args)
    if args.command == "auth" and args.auth_command == "mobile-discover":
        return _run_mobile_discovery()
    if args.command == "auth" and args.auth_command == "measure":
        if (
            args.interval_seconds < 1
            or args.duration_minutes < 1
            or (
                args.idle_minutes is not None
                and not 1 <= args.idle_minutes <= args.duration_minutes
            )
        ):
            build_parser().error("Use positive times; idle must not exceed duration.")
        return _run_measurement(args)
    if args.command == "auth":
        return _run_auth(
            args.auth_command,
            verify=getattr(args, "verify", False),
        )

    if args.command == "serve":
        asyncio.run(serve_stdio(WebQrOptions(args.auth_host, args.auth_port, args.auth_public_url)))
        return 0
    print("The sync command is scaffolded; its implementation is pending change tracking.")
    return 2


def _run_auth(command: str, verify: bool = False) -> int:
    store = SessionStore()
    try:
        with store.locked():
            return _run_locked_auth(store, command, verify)
    except SessionLockTimeout as error:
        print(f"Authentication busy: {error}")
        return 1


def _run_locked_auth(store: SessionStore, command: str, verify: bool) -> int:
    if command == "status":
        session = store.load()
        if session is None:
            print("No authenticated session is stored.")
            return 0
        if not verify:
            print("Authenticated session is stored (not verified against InfoMentor).")
            return 0
        http = InfoMentorHttpClient(session)
        try:
            http.verify_authenticated_read()
        except InfoMentorError as error:
            print(f"Stored session could not be verified: {error}")
            return 1
        finally:
            http.close()
        print("Stored session was restored and verified with a read-only InfoMentor request.")
        return 0
    if command == "logout":
        store.clear()
        print("Local authenticated session removed.")
        print("Mobile credentials are separate; revoke them with auth mobile-revoke --confirm.")
        return 0

    raise ValueError("Unknown authentication command.")


def _run_login(args: argparse.Namespace) -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def run() -> int:
        store = SessionStore()
        if args.method == "web-qr":
            attempt = LoginAttempt(
                store, "web-qr", WebQrOptions(args.host, args.port, args.public_url)
            )
            try:
                started = await attempt.start()
                if "login_url" in started:
                    print("Öppna QR-visningen på en annan enhet än BankID-telefonen:", flush=True)
                    print(started["login_url"], flush=True)
                    if args.host not in {"127.0.0.1", "::1"}:
                        print(
                            "HTTP över betrott LAN: exponera inte porten mot internet.", flush=True
                        )
                await attempt.task
                if attempt.state != "complete":
                    print(f"Authentication stopped: {attempt.snapshot()}")
                    return 1
            finally:
                await attempt.cancel()
        else:
            async with store.async_locked():
                session = await (url.login() if args.method == "url" else terminal_qr.login())
                if args.probe_sso_renewal:
                    session = _probe_sso_renewal(session)
                store._save(session)
            try:
                await complete_mobile_login(store, session)
            except Exception:
                print("Web login saved, but mobile setup failed; session renewal is not bound.")
                return 1
        print("BankID login completed; the saved session is bound for mobile renewal.")
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("Login cancelled locally; the upstream BankID order may remain until it expires.")
        return 130
    except (
        InfoMentorError, httpx.HTTPError, TimeoutError, OSError, ValueError, SessionLockTimeout
    ):
        print("Authentication failed or timed out; credentials and upstream URLs withheld.")
        return 1
    except Exception:
        print("Authentication failed unexpectedly; sensitive details withheld.")
        return 1


def _run_mobile_flow(args: argparse.Namespace) -> int:
    store = SessionStore()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    flow = None
    try:
        with store.locked():
            flow = MobilePairing(store)
            if args.auth_command == "mobile-pair":
                flow.pair()
                print("Mobile pairing stored; token-based fresh Hub session verified.")
            elif args.auth_command == "mobile-test":
                flow.test(refresh=args.refresh, after_expiry=args.after_expiry)
                print(
                    "Expired mobile access token, refresh exchange and fresh Hub session verified."
                    if args.after_expiry else (
                        "Mobile refresh exchange and fresh Hub session verified."
                        if args.refresh else "Mobile-token fresh Hub session verified."
                    )
                )
            else:
                flow.revoke()
                print(
                    "Refresh credential rejected after revocation check; "
                    "local mobile credentials removed."
                )
        return 0
    except InfoMentorError as error:
        # Some existing HTTP helper errors contain redirect paths. Do not echo
        # them from this credential-bearing experiment.
        from infomentor_mcp.auth.mobile_pairing import MobileFlowError

        print(
            str(error)
            if isinstance(error, MobileFlowError)
            else "Mobile flow authentication failed."
        )
    except SessionLockTimeout:
        print("Mobile flow stopped: session busy; retry after the other operation finishes.")
    except httpx.HTTPError:
        print("Mobile flow HTTP/network failure; details withheld. Stored credentials retained.")
    except (ValueError, OSError):
        print("Mobile flow local data/storage failure; details withheld.")
    except Exception:
        # Keyring implementations can raise backend-specific exceptions with
        # credential-bearing diagnostics; no traceback is safe here.
        print("Mobile flow failed; details withheld. Check local credential availability.")
    finally:
        if flow is not None:
            flow.close()
    return 1


def _run_mobile_discovery() -> int:
    store = SessionStore()
    http = None
    stage = "session_load"
    # No HTTP library diagnostics may accidentally echo signed redirect URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        with store.locked():
            session = store.load()
            if session is None:
                print("No saved session. Run infomentor-mcp auth login first.")
                return 1
            http = InfoMentorHttpClient(session)
            stage = "hub_session_check"
            status = http.verify_authenticated_read()
            print(f"Mobile discovery: Hub read verified (HTTP {status}).", flush=True)
            stage = "mobile_discovery"
            print(
                "Experimental: requesting discovery metadata only. "
                "No device pairing, OAuth exchange, or discovered URL will be followed.",
                flush=True,
            )
            try:
                report = discover_mobile_auth(http)
            finally:
                # Preserve only ordinary rotated web cookies, never discovery JSON.
                store.save(http.session())
            print(report.model_dump_json(indent=2))
            print("Discovery does not establish Stockholm mobile OAuth or refresh-token support.")
            return 0 if report.complete_expected_shape else 2
    except MobileDiscoveryError as error:
        print(
            f"Mobile discovery stopped: stage={stage}, code={error.code}, "
            f"HTTP={error.http_status}. {error}"
        )
    except AuthenticationExpiredError as error:
        print(
            f"Mobile discovery stopped: stage={stage}, code=authentication_required, "
            f"HTTP={error.http_status}. Run infomentor-mcp auth login first."
        )
    except SessionLockTimeout:
        print("Mobile discovery stopped: code=session_busy; retry when other operations finish.")
    except httpx.HTTPStatusError as error:
        print(f"Mobile discovery stopped: stage={stage}, HTTP={error.response.status_code}.")
    except httpx.RequestError:
        print(f"Mobile discovery stopped: stage={stage}, code=network_error; details withheld.")
    except InfoMentorError:
        print(
            f"Mobile discovery stopped: stage={stage}, code=unexpected_response; details withheld."
        )
    except ValueError:
        print(
            f"Mobile discovery stopped: stage={stage}, code=invalid_local_data; details withheld."
        )
    except OSError:
        print(
            f"Mobile discovery stopped: stage={stage}, code=local_storage_error; details withheld."
        )
    finally:
        if http is not None:
            http.close()
    return 1


def _run_measurement(args: argparse.Namespace) -> int:
    store = SessionStore()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = args.log or store.fallback_path.parent / f"lifetime-{stamp}.jsonl"
    try:
        log = MeasurementLog(path)
    except OSError:
        print("Cannot create measurement log. Choose a new writable --log path.")
        return 1
    http = InfoMentorHttpClient()
    try:
        print(f"Measurement log: {path}", flush=True)
        print(
            "The session will be locked for the entire test. "
            "Other local MCP/auth operations will wait or return busy.",
            flush=True,
        )
        with store.locked():
            SamlSession(http).login(method=args.method)
            http.verify_authenticated_read()
            store.save(http.session())
            outcome = measure_session(
                http, store, log.emit,
                interval=args.interval_seconds,
                duration=args.duration_minutes * 60,
                idle=args.idle_minutes * 60 if args.idle_minutes is not None else None,
            )
        return 0 if outcome in {"bankid_required", "duration_reached", "idle_check_complete"} else 1
    except (InfoMentorError, SessionLockTimeout):
        log.emit({"event": "setup_failed", "reason": "authentication_or_lock_failure"})
        return 1
    except httpx.HTTPError:
        log.emit({"event": "setup_failed", "reason": "http_failure"})
        return 1
    except KeyboardInterrupt:
        log.emit({"event": "interrupted"})
        return 130
    finally:
        http.close()
        log.close()


def _probe_sso_renewal(session):
    """Test SSO continuity in an isolated client without risking the active session."""
    probe_http = InfoMentorHttpClient(session.stockholm_sso_only())
    try:
        renewed = SamlSession(probe_http).resume_from_stockholm_sso()
        if not renewed:
            print("SSO renewal probe: BankID is required again.")
            return session
        probe_http.verify_authenticated_read()
        print("SSO renewal probe: Hub session was renewed without another BankID approval.")
        return probe_http.session()
    except InfoMentorError as error:
        print(f"SSO renewal probe was inconclusive: {error}")
        return session
    finally:
        probe_http.close()
