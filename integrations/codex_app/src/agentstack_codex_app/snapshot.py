"""Sanitized runtime snapshot persistence boundary."""


def write_snapshot(snapshot) -> None:
    """Atomically persist non-secret runtime metadata in a later P1 step."""

    raise NotImplementedError("snapshot writer is not implemented in this scaffold")
