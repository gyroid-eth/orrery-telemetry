"""Codex lifecycle hook entry point.

Implementation is intentionally blocked until the remaining in-App P0 verifies
hook firing, stable session IDs, and bundled MCP process scope.
"""


def main() -> None:
    """Receive and spool a lifecycle event after the in-App P0 is complete."""

    raise NotImplementedError("Codex App hook handling is gated on the in-App P0")
