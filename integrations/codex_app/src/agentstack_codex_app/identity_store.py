"""Durable binding and separate owner-secret storage boundary."""


class IdentityStore:
    """P1 implementation boundary for token-safe external-ID bindings."""

    def resolve(self, external_id: str):
        raise NotImplementedError("identity storage is not implemented in this scaffold")
