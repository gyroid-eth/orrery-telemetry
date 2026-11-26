# Phase two: instructions to a test fresh Mail instance (explicit opt-in)

This separate optional messaging adapter follows the [agreement on Issue #395](https://github.com/gyroid-eth/orrery-telemetry/issues/395#issuecomment-7195231301). Until production fresh cutover is accepted, use a test Mail instance only. The regular installer, daemon, UI, production services and public Mail API are unchanged.

The existing `python -m orrery_chatgpt_mcp` and `python -m orrery_chatgpt_mcp.server` entrypoints remain read-only. The separate `python -m orrery_chatgpt_mcp.fresh_server` entrypoint also defaults to disabled messaging and requires explicit fresh-only configuration. Service installation, distribution, spawning and automatic migration of old senders or credentials are outside this phase.

## Fixed authority

Use a protected fresh client context already prepared by the operator for the test environment. Sending calls `FreshBoundClient.call('send_message', ..., request_id=...)`, the same implementation as `agentstack_mail.fresh_client call-json --context <fixed-context> --tool send_message --request-id <UUID>`. The adapter does not create or enroll a context, reimplement credentials or directly read/write the Mail database.

A private policy fixes the Mail instance, stable sender ID and 1–16 recipients. Each recipient specifies `target_agent_id`, `expected_target_name` and `kind=to`. Each request has one recipient; there is no name-only selection or fallback to another ID with the same name. More than 16 entries is a configuration error without deleting, truncating or replacing entries. MCP inputs cannot select contexts, credentials, endpoints, senders, projects or request UUIDs.

Preview-time whois does not replace authorization at send time. Mail's transaction checks current credentials, recipient identity and expected name, retirement and contact policy for new sends. The configured Dashboard must identify the same fresh Mail instance.

This feature delegates send authority from the operator to the connected client. ChatGPT confirmation and the preview/commit sequence are not server-verified human approval. Anyone with the private URL and Tunnel access shares the policy's send authority. The recipient allowlist limits sending; fresh read tools still expose the selected instance's full roster.

## Manual operation in a test environment

Prepare this checkout's Mail package and dependencies in the [development venv](../../CONTRIBUTING.md#test-environment). Do not repurpose an existing service venv. Create a policy using the operator-verified test context, instance, sender ID, recipient ID and name. These values illustrate the format and must be replaced with verified values.

```json
{
  "kind": "orrery-mcp-fresh-message-policy-v1",
  "context_path": "/absolute/private/test-fresh/clients/confirmed-client-uuid/context.json",
  "mail_instance_id": "0123456789abcdef0123456789abcdef",
  "sender_agent_id": 1,
  "recipients": [
    {"target_agent_id": 2, "expected_target_name": "ConfirmedRecipient", "kind": "to"}
  ]
}
```

Use an owner-owned 0600 regular policy file and a dedicated 0700 state directory at an absolute canonical path. Symlinks, hardlinks and duplicate JSON keys are refused. Do not reuse an existing read-only state directory or secret URL for sending. From the checkout root, use a new terminal with a free loopback port and the test Dashboard.

```sh
export PYTHONPATH="$PWD/integrations/chatgpt_mcp/src:$PWD/packages/agentstack_mail/src"
export ORRERY_MCP_MODE=fresh
unset ORRERY_MCP_PROJECT_KEY
export ORRERY_MCP_STATE_DIR=/absolute/private/test-mcp-state
export ORRERY_MCP_FRESH_MESSAGE_POLICY=/absolute/private/test-message-policy.json
export ORRERY_MCP_DASHBOARD_URL=http://127.0.0.1:8770
export ORRERY_MCP_PORT=8792
export ORRERY_MCP_ENABLE_SPAWN=0
ORRERY_MCP_ENABLE_MESSAGE=0 .venv/bin/python -m orrery_chatgpt_mcp pair --show-url
export ORRERY_MCP_ENABLE_MESSAGE=1
.venv/bin/python -m orrery_chatgpt_mcp.fresh_server check
.venv/bin/python -m orrery_chatgpt_mcp.fresh_server serve
```

Set the Dashboard URL to the actual test listener and do not select an occupied port. Check verifies fixed authority and Dashboard instance without sending. Serve validates before binding only 127.0.0.1. Changed or revoked policy/context authority is refused before a new send.

For a Tunnel, follow the [existing connection procedure](https://github.com/gyroid-eth/orrery/blob/master/docs/en/chatgpt-mcp.md#secure-mcp-tunnel-configuration) with a dedicated Tunnel for this test endpoint. Verify organization/workspace ownership, shared users/groups, the runtime-key principal's Read/Use permissions and ChatGPT connection sharing. Preserve the secret path and fixed Host; do not disable Origin checks. Run the linked guide's URL-extraction Python command with `ORRERY_MCP_ENABLE_MESSAGE=0` for that one invocation only. This does not change the running fresh process's configuration. Verify Mail's effective body-log setting and keep Tunnel logs owner-private because they may contain secret URLs. The adapter does not change other processes' log settings.

## Preview, commit and status

When enabled, the gateway adds `orrery_message_preview`, `orrery_message_commit` and `orrery_message_status` to the three read tools. Preview accepts only `idempotency_key`, `mail_instance_id`, `target_agent_id`, `expected_target_name`, `subject` and `body_md`. It returns the recipient, subject, complete body, digest, preview ID, request UUID and a five-minute expiry. The UTF-8 limits are 200 bytes for the subject and 65536 for the body. Empty values and control characters are refused except body LF/TAB.

Commit accepts only the same key, preview ID and digest; it cannot replace the body or recipient. Different content cannot reuse a key. Before contacting Mail, the adapter durably stores the key/UUID mapping, full content and digest, and send claim in a private SQLite ledger. Concurrent commits, separate processes and restarts do not resend a claimed request. UUIDs derive from the instance, sender and key hash; this does not make deleting the ledger safe.

Accepted means the protected send response matches the saved content's instance, sender, recipient and message ID, includes `decision_only=false`, `delivery_status=notification_pending`, thread and timestamp, and its receipt was saved. The normal send transaction stores the body, recipients and request outcome together. Empty or decision-only responses are not accepted. Notification delivery and agent execution are separate; `execution_status=unobserved` remains unchanged.

Status reads only the saved local ledger and receipt. It does not contact Mail or the Dashboard, call a receipt API, replay, send or resend; its `readOnlyHint=true`. Lost responses and interruptions without conclusive evidence remain unknown. Repeated status or commit calls do not re-execute them. This status does not automatically observe later operator inspection or recovery on Mail.

Ledger reads verify the fixed policy, saved content and recipient, digest, key-derived UUID, state, expiry and receipt consistency. Inconsistent evidence raises `message_state_invalid`. These checks are not signatures against arbitrary changes by the same OS user. The ledger refuses new previews at 10000 requests without automatically deleting history. Both the ledger and protected client requests contain message bodies: preserve their 0700/0600 protection and retain the evidence. Credentials are not copied into the ledger, and commit/status omit bodies and local paths.

## Stop, recover and verify disabled operation

Stop this test foreground process with Ctrl+C. To disable messaging, restart it with `ORRERY_MCP_ENABLE_MESSAGE=0`. Editing environment variables or a policy file alone does not establish that an already running process is disabled.

With the connected MCP client, initialize, send notifications/initialized, list tools and call orrery_status. Disabled operation exposes only three read tools and reports `read_only=true`, `spawn_enabled_on_server=false` and `message_enabled_on_server=false`. A direct commit call returns `isError=true` with `error.code=unknown_tool`. An unauthenticated 404 or healthz alone is not proof of disabled sending. Enabled operation must expose six tools, message flag=true, spawn flag=false and the intended sender/recipient authority.

For unknown outcomes or ledger inconsistency, retain the ledger and client requests while the operator verifies the original request and receipt evidence. Replay can perform the first send when the original never arrived, so it is not a status check. Re-execution requires an explicit operator decision; the adapter does not provide recovery sends. Do not automatically recover by deleting the ledger, choosing another key or truncating an oversized policy.

## Verification boundary

Tests use temporary fresh roots, synthetic registrations, dedicated dynamic ports and isolated HOME values. They cover fixed authority, body/recipient storage, UUIDs, restart, response loss, concurrent commit, retirement, unknown outcomes, ledger corruption, disabled-state refusal and MCP SDK connections. They do not establish real ChatGPT sending, hosted Tunnel ACLs or actual agent execution.
