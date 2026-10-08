-- Candidate only; the legacy initializer continues to apply 001_delivery_state.sql.
CREATE TABLE codex_app_delivery_state(
mail_instance_id TEXT NOT NULL, agent_id INTEGER NOT NULL CHECK(agent_id>0), message_id INTEGER NOT NULL CHECK(message_id>0),
status TEXT NOT NULL CHECK(status IN ('pending','leased','delivered','failed','dead_letter')),
lease_owner TEXT, lease_expires_at TEXT, attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),last_error TEXT,
created_at TEXT NOT NULL,updated_at TEXT NOT NULL,delivered_at TEXT,legacy_project_key TEXT,legacy_agent_name TEXT,
policy_json TEXT NOT NULL,next_attempt_at TEXT NOT NULL,coalesce_ready_at TEXT NOT NULL,
PRIMARY KEY(mail_instance_id,agent_id,message_id),
CHECK((status='leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status<>'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)),
CHECK((status='delivered' AND delivered_at IS NOT NULL) OR (status<>'delivered' AND delivered_at IS NULL)));

CREATE TRIGGER namespace_delivery_status_transition
BEFORE UPDATE OF status ON codex_app_delivery_state
WHEN NOT (OLD.status=NEW.status
OR (OLD.status='pending' AND NEW.status IN ('leased','dead_letter'))
OR (OLD.status='leased' AND NEW.status IN ('pending','delivered','failed','dead_letter'))
OR (OLD.status='failed' AND NEW.status IN ('pending','leased','dead_letter')))
BEGIN SELECT RAISE(ABORT,'invalid candidate delivery transition'); END;
