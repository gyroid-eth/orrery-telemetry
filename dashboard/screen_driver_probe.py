"""Check the Windows Cua Driver pipe using the installed Mail relay."""
import asyncio
from contextlib import AsyncExitStack
import os
import time

_started = time.monotonic()


def _phase(name):
    elapsed_ms = round((time.monotonic() - _started) * 1000)
    print(f"ORRERY_PROBE|{name}|{elapsed_ms}|", flush=True)


DRIVER_NOT_FOUND = 2
DRIVER_UNAVAILABLE = 3
DRIVER_INCOMPATIBLE = 4


async def probe(parameters):
    from agentstack_mail.screen_mcp import Relay, SCHEMAS, ScreenError

    relay = Relay(SCHEMAS["windows"], None, lambda: parameters, timeout=3)
    async with AsyncExitStack() as stack:
        try:
            await relay._open(stack)
        except ScreenError:
            return DRIVER_INCOMPATIBLE
        _phase("handshake")
        async with asyncio.timeout(2):
            # The pipe must answer a read-only call, without a prompt or screen capture.
            result = await relay.session.call_tool("check_permissions", {"prompt": False})
        _phase("permission")
        effect = (result.structuredContent or {}).get("effect")
        if result.isError or effect in ("refused", "partial", "suspected_noop", "unverifiable"):
            return DRIVER_UNAVAILABLE
        return 0


def main():
    _phase("started")
    from agentstack_mail.screen_mcp import ScreenError, driver_parameters

    _phase("mailrelay")
    try:
        parameters = driver_parameters("windows")
    except ScreenError:
        return DRIVER_NOT_FOUND
    if not os.path.isfile(parameters.command) or not os.access(parameters.command, os.X_OK):
        return DRIVER_NOT_FOUND
    _phase("driverresolved")
    try:
        return asyncio.run(probe(parameters))
    except Exception:
        # Protocol diagnostics can contain local paths or values from the driver.
        return DRIVER_UNAVAILABLE
    finally:
        _phase("finished")


if __name__ == "__main__":
    raise SystemExit(main())
