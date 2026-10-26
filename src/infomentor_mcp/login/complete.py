"""Finish an explicit BankID login with verified, persistent mobile access."""

import asyncio

from infomentor_mcp.auth.mobile import MobileAuthorization
from infomentor_mcp.errors import MobileFlowError
from infomentor_mcp.storage.session import AuthSession, SessionStore


async def complete_mobile_login(store: SessionStore, session: AuthSession) -> None:
    """Run the existing sync mobile protocol without blocking the async login/UI.

    Called after releasing the async flock. Recheck the saved session under the
    sync lock so another process cannot have its newer session overwritten.
    """
    def finish():
        with store.locked(timeout=0):
            if store.load() != session:
                raise MobileFlowError("Saved login changed before mobile setup; retry login.")
            with MobileAuthorization(store) as authorization:
                try:
                    authorization.complete_login()
                except Exception:
                    # Keep the verified BankID web login usable. Granted/rotated mobile
                    # credentials remain in separate storage for recovery or revocation.
                    store.save(session)
                    raise

    worker = asyncio.create_task(asyncio.to_thread(finish))
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            # A synchronous grant/rotation cannot safely be abandoned midway.
            # Even repeated cancellation must wait for persistence and lock release.
            continue
    worker.result()
