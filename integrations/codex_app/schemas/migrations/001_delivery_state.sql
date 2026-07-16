-- Delivery is at-least-once. The composite primary key is the idempotency key.
CREATE TABLE IF NOT EXISTS codex_app_delivery_state (
    project_key TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    message_id INTEGER NOT NULL CHECK (message_id > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'delivered', 'failed', 'dead_letter')),
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    delivered_at TEXT,
    PRIMARY KEY (project_key, agent_name, message_id),
    CHECK (
        (status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status = 'delivered' AND delivered_at IS NOT NULL)
        OR
        (status <> 'delivered' AND delivered_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS codex_app_delivery_ready_idx
    ON codex_app_delivery_state (status, lease_expires_at, updated_at);

-- Legal transitions make lease acquisition explicit and delivered/dead-letter
-- rows terminal. A manual retry resets failed -> pending before leasing again.
CREATE TRIGGER IF NOT EXISTS codex_app_delivery_status_transition
BEFORE UPDATE OF status ON codex_app_delivery_state
WHEN NOT (
    OLD.status = NEW.status
    OR (OLD.status = 'pending' AND NEW.status IN ('leased', 'dead_letter'))
    OR (OLD.status = 'leased' AND NEW.status IN ('pending', 'delivered', 'failed', 'dead_letter'))
    OR (OLD.status = 'failed' AND NEW.status IN ('pending', 'dead_letter'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid codex app delivery status transition');
END;
