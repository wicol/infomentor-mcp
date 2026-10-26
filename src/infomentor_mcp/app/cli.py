"""Command-line interface for InfoMentor MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from infomentor_mcp.app.mcp import McpApp, quiet_http_logging
from infomentor_mcp.app.service import InfoMentorService
from infomentor_mcp.auth.discovery import MobileDiscovery
from infomentor_mcp.auth.measurement import MeasurementLog, SessionLifetimeProbe
from infomentor_mcp.auth.mobile import MobileAuthorization
from infomentor_mcp.auth.saml import SamlFlow
from infomentor_mcp.errors import (
    AuthenticationExpiredError,
    InfoMentorError,
    MobileDiscoveryError,
    MobileFlowError,
)
from infomentor_mcp.hub.transport import HubTransport
from infomentor_mcp.login.attempt import LoginAttempt
from infomentor_mcp.login.complete import complete_mobile_login
from infomentor_mcp.login.terminal import TerminalLogin
from infomentor_mcp.login.web_qr import WebQrOptions
from infomentor_mcp.storage.lock import SessionLockTimeout
from infomentor_mcp.storage.session import AuthSession, SessionStore


class Cli:
    """infomentor-mcp commands; each subcommand is one method returning an exit code."""

    def __init__(self, store: SessionStore | None = None) -> None:
        self.store = store or SessionStore()
        self.parser = self._build_parser()

    def run(self, argv: Sequence[str] | None = None) -> int:
        args = self.parser.parse_args(argv)
        quiet_http_logging()
        return args.handler(args)

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="infomentor-mcp")
        subparsers = parser.add_subparsers(dest="command", required=True)

        auth = subparsers.add_parser("auth", help="Manage the authenticated InfoMentor session")
        auth_subparsers = auth.add_subparsers(dest="auth_command", required=True)
        login = auth_subparsers.add_parser(
            "login", help="Log in with BankID and set up persistent mobile access",
            description="Log in with BankID, then automatically set up mobile session renewal.",
        )
        login.set_defaults(handler=self.login)
        login.add_argument(
            "--method",
            choices=("qr", "url", "web-qr"),
            default="qr",
            help="Terminal QR (default), BankID app URL, or temporary animated web QR page",
        )
        login.add_argument(
            "--host", default="127.0.0.1", help="Web QR listen IP; 0.0.0.0 for trusted LAN"
        )
        login.add_argument("--port", type=int, default=0, help="Web QR port; 0 chooses a free port")
        login.add_argument(
            "--public-url",
            help="Web QR HTTP origin reachable by your browser, e.g. http://login-host.local:8765",
        )
        login.add_argument(
            "--probe-sso-renewal",
            action="store_true",
            help="Immediately test whether Stockholm SSO can mint a new Hub session without BankID",
        )
        status = auth_subparsers.add_parser("status", help="Show local authentication status")
        status.set_defaults(handler=self.status)
        status.add_argument(
            "--verify",
            action="store_true",
            help="Restore the stored session in a new process and perform a read-only verification",
        )
        logout = auth_subparsers.add_parser(
            "logout", help="Remove locally stored authentication data",
        )
        logout.set_defaults(handler=self.logout)
        discover = auth_subparsers.add_parser(
            "mobile-discover",
            help="Experimental Hub app discovery using saved cookies; no pairing or token exchange",
        )
        discover.set_defaults(handler=self.mobile_discover)
        pair = auth_subparsers.add_parser(
            "mobile-pair",
            help="Experimental: explicitly authorize a new mobile client and store tokens",
        )
        pair.set_defaults(handler=self.mobile_flow)
        pair.add_argument("--confirm", action="store_true", required=True)
        test = auth_subparsers.add_parser(
            "mobile-test", help="Test mobile-token SSO into a fresh, verified Hub session"
        )
        test.set_defaults(handler=self.mobile_flow)
        test.add_argument(
            "--refresh", action="store_true", help="Explicitly rotate tokens before SSO",
        )
        test.add_argument(
            "--after-expiry", action="store_true",
            help="Wait for access expiry, require HTTP 401, then refresh and verify fresh Hub SSO",
        )
        revoke = auth_subparsers.add_parser(
            "mobile-revoke",
            help="Revoke the experimental refresh token before removing local credentials",
        )
        revoke.set_defaults(handler=self.mobile_flow)
        revoke.add_argument("--confirm", action="store_true", required=True)
        measure = auth_subparsers.add_parser(
            "measure", help="Fresh BankID login followed by automated session-lifetime measurement"
        )
        measure.set_defaults(handler=self.measure)
        measure.add_argument("--method", choices=("qr", "url"), default="qr")
        measure.add_argument("--interval-seconds", type=int, default=120)
        measure.add_argument("--duration-minutes", type=int, default=480)
        measure.add_argument(
            "--idle-minutes", type=int,
            help="Leave the fresh session untouched, check once after this delay, then exit",
        )
        measure.add_argument(
            "--log", type=Path, help="New private JSONL log; never overwrites a file",
        )

        serve = subparsers.add_parser("serve", help="Run the MCP server over stdio")
        serve.set_defaults(handler=self.serve)
        serve.add_argument("--auth-host", default="127.0.0.1")
        serve.add_argument("--auth-port", type=int, default=0)
        serve.add_argument("--auth-public-url")
        sync = subparsers.add_parser("sync", help="Fetch and record school-data changes as JSON")
        sync.set_defaults(handler=self.sync)
        sync.add_argument("--sources", nargs="+", choices=("news", "calendar"))
        sync.add_argument("--child-ids", nargs="+")
        sync.add_argument("--start-date", type=date.fromisoformat)
        sync.add_argument("--end-date", type=date.fromisoformat)
        sync.add_argument("--include-attachments", action=argparse.BooleanOptionalAction,
                          default=True)
        sync.add_argument("--request-delay-seconds", type=float, default=1.0)
        sync.add_argument("--consumer", default="default")
        sync.add_argument("--limit", type=int, default=100)
        changes = subparsers.add_parser(
            "changes", help="Read, acknowledge, or reset change tracking",
        )
        commands = changes.add_subparsers(dest="changes_command", required=True)
        get = commands.add_parser("get")
        get.set_defaults(handler=self.changes)
        get.add_argument("--consumer", default="default")
        get.add_argument("--limit", type=int, default=100)
        ack = commands.add_parser("ack")
        ack.set_defaults(handler=self.changes)
        ack.add_argument("--consumer", required=True)
        ack.add_argument("--cursor", required=True)
        reset = commands.add_parser("reset")
        reset.set_defaults(handler=self.changes)
        reset.add_argument("--confirm", action="store_true", required=True)
        return parser

    def serve(self, args: argparse.Namespace) -> int:
        options = WebQrOptions(args.auth_host, args.auth_port, args.auth_public_url)
        asyncio.run(McpApp(InfoMentorService(self.store), options).serve_stdio())
        return 0

    def sync(self, args: argparse.Namespace) -> int:
        return self._print_json(InfoMentorService(self.store).sync_changes(
            args.sources, args.child_ids, args.start_date, args.end_date,
            args.include_attachments, args.request_delay_seconds, args.consumer, args.limit,
        ))

    def changes(self, args: argparse.Namespace) -> int:
        service = InfoMentorService(self.store)
        if args.changes_command == "get":
            return self._print_json(service.get_changes(args.consumer, args.limit))
        if args.changes_command == "ack":
            return self._print_json(service.ack_changes(args.consumer, args.cursor))
        return self._print_json(service.reset_changes())

    def status(self, args: argparse.Namespace) -> int:
        try:
            with self.store.locked():
                session = self.store.load()
                if session is None:
                    print("No authenticated session is stored.")
                    return 0
                if not args.verify:
                    print("Authenticated session is stored (not verified against InfoMentor).")
                    return 0
                with HubTransport(session) as http:
                    http.verify_authenticated_read()
        except SessionLockTimeout as error:
            print(f"Authentication busy: {error}")
            return 1
        except InfoMentorError as error:
            print(f"Stored session could not be verified: {error}")
            return 1
        print("Stored session was restored and verified with a read-only InfoMentor request.")
        return 0

    def logout(self, args: argparse.Namespace) -> int:
        try:
            self.store.clear()
        except SessionLockTimeout as error:
            print(f"Authentication busy: {error}")
            return 1
        print("Local authenticated session removed.")
        print("Mobile credentials are separate; revoke them with auth mobile-revoke --confirm.")
        return 0

    def login(self, args: argparse.Namespace) -> int:
        if args.method == "web-qr" and args.probe_sso_renewal:
            self.parser.error("The SSO probe is supported with --method qr or url, not web-qr.")
        try:
            return asyncio.run(
                self._web_login(args) if args.method == "web-qr" else self._terminal_login(args)
            )
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

    async def _web_login(self, args: argparse.Namespace) -> int:
        attempt = LoginAttempt(
            self.store, "web-qr", WebQrOptions(args.host, args.port, args.public_url)
        )
        try:
            started = await attempt.start()
            if "login_url" in started:
                print("Öppna QR-visningen på en annan enhet än BankID-telefonen:", flush=True)
                print(started["login_url"], flush=True)
                if args.host not in {"127.0.0.1", "::1"}:
                    print("HTTP över betrott LAN: exponera inte porten mot internet.", flush=True)
            await attempt.task
            if attempt.state != "complete":
                print(f"Authentication stopped: {attempt.snapshot()}")
                return 1
        finally:
            await attempt.cancel()
        print("BankID login completed; the saved session is bound for mobile renewal.")
        return 0

    async def _terminal_login(self, args: argparse.Namespace) -> int:
        async with self.store.async_locked() as record:
            session = await TerminalLogin(args.method).run()
            if args.probe_sso_renewal:
                session = self._probe_sso_renewal(session)
            record.save(session)
        try:
            await complete_mobile_login(self.store, session)
        except Exception:
            print("Web login saved, but mobile setup failed; session renewal is not bound.")
            return 1
        print("BankID login completed; the saved session is bound for mobile renewal.")
        return 0

    def mobile_flow(self, args: argparse.Namespace) -> int:
        try:
            with self.store.locked(), MobileAuthorization(self.store) as authorization:
                if args.auth_command == "mobile-pair":
                    authorization.pair()
                    print("Mobile pairing stored; token-based fresh Hub session verified.")
                elif args.auth_command == "mobile-test":
                    authorization.test(refresh=args.refresh, after_expiry=args.after_expiry)
                    print(
                        "Expired mobile access token, refresh exchange and fresh Hub session "
                        "verified." if args.after_expiry else (
                            "Mobile refresh exchange and fresh Hub session verified."
                            if args.refresh else "Mobile-token fresh Hub session verified."
                        )
                    )
                else:
                    authorization.revoke()
                    print(
                        "Refresh credential rejected after revocation check; "
                        "local mobile credentials removed."
                    )
            return 0
        except MobileFlowError as error:
            print(error)
        except InfoMentorError:
            # Some HTTP errors contain redirect paths. Do not echo them from this
            # credential-bearing flow.
            print("Mobile flow authentication failed.")
        except SessionLockTimeout:
            print("Mobile flow stopped: session busy; retry after the other operation finishes.")
        except httpx.HTTPError:
            print(
                "Mobile flow HTTP/network failure; details withheld. Stored credentials retained."
            )
        except (ValueError, OSError):
            print("Mobile flow local data/storage failure; details withheld.")
        except Exception:
            # Keyring implementations can raise backend-specific exceptions with
            # credential-bearing diagnostics; no traceback is safe here.
            print("Mobile flow failed; details withheld. Check local credential availability.")
        return 1

    def mobile_discover(self, args: argparse.Namespace) -> int:
        stage = "session_load"
        try:
            with self.store.locked():
                session = self.store.load()
                if session is None:
                    print("No saved session. Run infomentor-mcp auth login first.")
                    return 1
                with HubTransport(session) as http:
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
                        report = MobileDiscovery(http).run()
                    finally:
                        # Preserve only ordinary rotated web cookies, never discovery JSON.
                        self.store.save(http.session())
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
            print(
                "Mobile discovery stopped: code=session_busy; retry when other operations finish."
            )
        except httpx.HTTPStatusError as error:
            print(f"Mobile discovery stopped: stage={stage}, HTTP={error.response.status_code}.")
        except httpx.RequestError:
            print(f"Mobile discovery stopped: stage={stage}, code=network_error; details withheld.")
        except InfoMentorError:
            print(
                f"Mobile discovery stopped: stage={stage}, code=unexpected_response; "
                "details withheld."
            )
        except ValueError:
            print(
                f"Mobile discovery stopped: stage={stage}, code=invalid_local_data; "
                "details withheld."
            )
        except OSError:
            print(
                f"Mobile discovery stopped: stage={stage}, code=local_storage_error; "
                "details withheld."
            )
        return 1

    def measure(self, args: argparse.Namespace) -> int:
        if (
            args.interval_seconds < 1 or args.duration_minutes < 1
            or (args.idle_minutes is not None
                and not 1 <= args.idle_minutes <= args.duration_minutes)
        ):
            self.parser.error("Use positive times; idle must not exceed duration.")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = args.log or self.store.state_dir / f"lifetime-{stamp}.jsonl"
        try:
            log = MeasurementLog(path)
        except OSError:
            print("Cannot create measurement log. Choose a new writable --log path.")
            return 1
        http = HubTransport()
        try:
            print(f"Measurement log: {path}", flush=True)
            print(
                "The session will be locked for the entire test. "
                "Other local MCP/auth operations will wait or return busy.",
                flush=True,
            )
            with self.store.locked():
                http.reset(asyncio.run(TerminalLogin(args.method).run()))
                http.verify_authenticated_read()
                self.store.save(http.session())
                outcome = SessionLifetimeProbe(
                    http, self.store, log.emit,
                    interval=args.interval_seconds,
                    duration=args.duration_minutes * 60,
                    idle=args.idle_minutes * 60 if args.idle_minutes is not None else None,
                ).run()
            completed = {"bankid_required", "duration_reached", "idle_check_complete"}
            return 0 if outcome in completed else 1
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

    @staticmethod
    def _print_json(result: dict) -> int:
        print(json.dumps(result, ensure_ascii=False))
        return 1 if "error" in result or result.get("status") == "partial" else 0

    @staticmethod
    def _probe_sso_renewal(session: AuthSession) -> AuthSession:
        """Test SSO continuity in an isolated client without risking the active session."""
        with HubTransport(session.stockholm_sso_only()) as probe:
            try:
                if not SamlFlow(probe).resume_stockholm_sso():
                    print("SSO renewal probe: BankID is required again.")
                    return session
                probe.verify_authenticated_read()
            except InfoMentorError as error:
                print(f"SSO renewal probe was inconclusive: {error}")
                return session
            print("SSO renewal probe: Hub session was renewed without another BankID approval.")
            return probe.session()


def main(argv: Sequence[str] | None = None) -> int:
    return Cli().run(argv)
