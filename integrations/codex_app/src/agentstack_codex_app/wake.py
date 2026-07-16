"""Cold-wake coordination boundary.

No wake or resume command is implemented in P1. Delivery leasing alone must
not activate an App task before the remaining in-App compatibility checks pass.
"""


class WakeCoordinator:
    """Placeholder for the P3 at-least-once cold-wake coordinator."""

    def wake(self, external_id: str, message_ids: list[int]) -> None:
        raise NotImplementedError("cold wake is gated on the in-App P0")
