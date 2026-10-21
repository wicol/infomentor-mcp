"""Same-device BankID link flow; no QR updates or terminal output."""

import asyncio

from infomentor_mcp.auth.stockholm_bankid import BankIdOrder, StockholmBankId


async def wait_for_approval(protocol: StockholmBankId, order: BankIdOrder):
    async with asyncio.timeout(240):
        while True:
            await asyncio.sleep(1)
            status = await protocol.poll(order)
            if status.state == "OK":
                return await protocol.finish(order)


async def login():
    """Direct CLI path: print the explicit sensitive link once, then await approval."""
    protocol = StockholmBankId()
    try:
        async with asyncio.timeout(300):
            order = await protocol.begin("url")
            print("Öppna engångslänken i BankID på telefonen. Dela den inte.", flush=True)
            print(order.launch_url, flush=True)
            print("Auth: väntar på BankID, högst fyra minuter.", flush=True)
            return await wait_for_approval(protocol, order)
    except Exception:
        print(f"Auth: failed at {protocol.stage}; HTTP diagnostics: {protocol.diagnostics}")
        raise
    finally:
        await protocol.close()
