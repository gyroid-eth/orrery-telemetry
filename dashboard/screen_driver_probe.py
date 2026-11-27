"""Check the Windows Cua Driver pipe using the installed Mail relay."""
import asyncio
from contextlib import AsyncExitStack
import os

from agentstack_mail.screen_mcp import Relay, SCHEMAS, ScreenError, driver_parameters


DRIVER_NOT_FOUND = 2
DRIVER_UNAVAILABLE = 3
DRIVER_INCOMPATIBLE = 4


async def probe(parameters):
    relay = Relay(SCHEMAS["windows"], None, lambda: parameters, timeout=3)
    async with AsyncExitStack() as stack:
        try:
            await relay._open(stack)
        except ScreenError:
            return DRIVER_INCOMPATIBLE
        async with asyncio.timeout(2):
            # The pipe must answer a read-only call, without a prompt or screen capture.
            result = await relay.session.call_tool("check_permissions", {"prompt": False})
        effect = (result.structuredContent or {}).get("effect")
        if result.isError or effect in ("refused", "partial", "suspected_noop", "unverifiable"):
            return DRIVER_UNAVAILABLE
        return 0


def main():
    try:
        parameters = driver_parameters("windows")
    except ScreenError:
        return DRIVER_NOT_FOUND
    if not os.path.isfile(parameters.command) or not os.access(parameters.command, os.X_OK):
        return DRIVER_NOT_FOUND
    try:
        return asyncio.run(probe(parameters))
    except Exception:
        # Protocol diagnostics can contain local paths or values from the driver.
        return DRIVER_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
