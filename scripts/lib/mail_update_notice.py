#!/usr/bin/env python3
"""The one wording for "ORRERY Mail is out of date" and "Mail was just stopped".

install.sh, agentstack-doctor and the cockpit's setup.sh all print these
lines; they call this file instead of keeping copies, so the advice cannot
drift apart. Plain text, English, one screen.

    mail_update_notice.py stale [--running ID] [--to ID] [--missing tool.param,...]
                                [--mcp-url URL] [--features FILE] [--without-how]
    mail_update_notice.py after-stop [--outage SECONDS]

`stale` lists what the running Mail cannot do: the names given with
--missing, or, with --mcp-url, whatever its tools/list lacks from the
features file. --without-how leaves out the two "To update" lines, for a
caller that words its own (setup.sh, whose one-line form differs). `after-stop` is what to check once Mail answers again after a
switch, a rollback or an interrupted switch.

The /mcp steps are the Claude Code screens as they are (v2.1.287): /mcp lists
the servers, a dropped one shows "✘ failed", and its menu offers
"1. Authenticate", "2. Reconnect", "3. Disable". Reconnect is the one.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
FEATURES = HERE / "mail_required_features.json"

RECONNECT = ("in that session: /mcp, choose orrery-mail (shown as ✘ failed), "
             "then Reconnect (not Authenticate)")


def _features(path: pathlib.Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))["features"]
    except (OSError, ValueError, KeyError):
        return []


def _missing_from_server(url: str, wanted: list[dict]) -> list[str] | None:
    body = json.dumps({"jsonrpc": "2.0", "id": "mail-update-notice", "method": "tools/list",
                       "params": {}}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read().decode("utf-8", errors="replace")
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
        tools = {tool.get("name"): set((tool.get("inputSchema") or {}).get("properties") or {})
                 for tool in json.loads(raw)["result"]["tools"]}
    except Exception:  # an unreachable Mail is "unknown", never "complete"
        return None
    return [f"{f['tool']}.{f['parameter']}" for f in wanted
            if f["parameter"] not in tools.get(f["tool"], set())]


def _short(build: str) -> str:
    """A commit id is shortened; any other build name is shown as it is."""
    return build[:7] if len(build) >= 12 and all(c in "0123456789abcdef" for c in build) else build


def stale(args: argparse.Namespace) -> list[str]:
    wanted = _features(pathlib.Path(args.features))
    if args.missing is not None:
        missing = [name for name in args.missing.split(",") if name]
    elif args.mcp_url:
        missing = _missing_from_server(args.mcp_url, wanted)
    else:
        missing = None
    known = {f"{f['tool']}.{f['parameter']}": f for f in wanted}
    builds = ", ".join(part for part in (
        f"running {_short(args.running)}" if args.running else "",
        f"this checkout {_short(args.to)}" if args.to else "") if part)
    lines = [f"ORRERY Mail is out of date{f' ({builds})' if builds else ''}."]
    if missing:
        for name in missing:
            feature = known.get(name)
            if feature:
                lines.append(f"  Not working until it is updated: {feature['needed_for']} "
                             f"(now {feature['without_it']}).")
            else:
                lines.append(f"  Not working until it is updated: {name}.")
    else:
        lines.append("  Fixes and features in the newer Mail have not reached you yet.")
    if not args.without_how:
        lines += [
            "  To update: ./scripts/install.sh --mail update   (orrery-telemetry checkout)",
            "         or: ./scripts/setup.sh --mail update     (ORRERY cockpit checkout)",
        ]
    lines += [
        "  Risk: Mail stops for a few seconds while it switches, and agents' Mail calls fail meanwhile.",
        "        Children and Codex reconnect by themselves. A top-level Claude Code session may not",
        "        reconnect, and did not in testing when the stop lasted 18 s or more.",
        f"  If one shows orrery-mail as failed afterwards, {RECONNECT}.",
    ]
    return lines


def after_stop(args: argparse.Namespace) -> list[str]:
    return [
        f"ORRERY Mail was unavailable for {args.outage or '?'}s.",
        "  In each running Claude Code session, check that /mcp shows orrery-mail as connected and that"
        " a small call such as health_check succeeds;",
        f"  if it shows orrery-mail as failed: {RECONNECT}. Children (through the proxy) and Codex"
        " reconnect on their next call.",
        "  A Mail call that failed meanwhile may still have been carried out (only the answer was lost):"
        " check before repeating it --",
        "  a send in the recipient's inbox or your outbox, a registration with whois, a spawn or resume"
        " in the dashboard.",
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="kind", required=True)
    old = sub.add_parser("stale")
    old.add_argument("--running", default="")
    old.add_argument("--to", default="")
    old.add_argument("--missing", default=None)
    old.add_argument("--mcp-url", default="")
    old.add_argument("--features", default=str(FEATURES))
    old.add_argument("--without-how", action="store_true")
    old.set_defaults(render=stale)
    stop = sub.add_parser("after-stop")
    stop.add_argument("--outage", default="")
    stop.set_defaults(render=after_stop)
    args = parser.parse_args(argv)
    print("\n".join(args.render(args)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
