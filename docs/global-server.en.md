# Global server S1 preparation

[日本語](global-server.md)

S1 is an isolated real HTTP and Unix management-socket server for #213 client preparation. `agentstack-mail` without extra arguments retains the legacy server and its 25 tools. Only `--global-config <JSON> --host 127.0.0.1 --port <unused port>` selects S1. The PR3 pointer and database activation remain false. No installed service or user HOME is switched.

## Wire contract

The source of truth is `fixtures/global-server-s1.json` in the package and the actual signatures in `global_server.py`. Discover capabilities through `tools/list`; do not fall back when a required capability is absent.

Health returns the stable `server_instance_id`, `candidate_generation`, `authority_epoch`, `mutation_revision`, supported tools and unsupported resources. Owner operations require `expected_server_instance_id`, `candidate_generation`, `authority_epoch`, numeric `agent_id` and `registration_token`. Optional legacy `project_key` is ignored. Names, cwd and inherited tmux project settings cannot select the owner.

Registration authenticates an existing ID/token and returns its canonical name and credential generation after an approved rename. Reconnection can supply both `window_row_id` and `window_uuid` for owner verification. An existing identity without an owner token is never claimed by silently generating one. New registration omits the ID and rejects lookup/name conflicts; it creates no project membership. `whois` and inbox expose only the authenticated identity and its recipient rows. BCC peers and credentials are excluded. Inbox does not clear signals or mutate read/ack state in S1. Retirement and restoration require the same owner token. `ensure_project` creates nothing.

Three generations are distinct: candidate generation is the PR3 candidate fingerprint; mutation revision is the database `write_generation` counter; authority epoch belongs to the common runtime fence. Ordinary writes advance only the counter.

## Isolated configuration and fence

Configuration is an owner-private 0600 JSON with `kind=orrery-global-server-s1`, `activation_enabled=false`, `isolation_root`, `runtime_root`, `database`, `mail_instance_id`, `candidate_generation`, `authority_epoch`, `authority`, `authority_lock` and `management_socket`. All paths are absolute. Database, authority, lock and socket must reside in an owner-private 0700 temporary root. The server opens only a PR3 schema-2 candidate and never initializes the legacy ORM schema against it. Normal HOME database paths are rejected.

Authority contains `kind=orrery-global-authority-v1`, `phase=active`, `root_status=active`, `runtime_root` (the target state root within the temporary isolation directory), instance, candidate generation and authority epoch. The updater must hold an exclusive lock on the same private regular lock file when changing authority. The server holds a shared lock across the operation and SQLite transaction and rechecks authority before commit. Quiescing, retired roots, stale epochs and replacement of config/database/lock are rejected.

This fences cooperating writers. OS supervisor shutdown, complete writer inventory, old connection termination and installed pointer handoff remain PR7 work. An unknown process with direct database access is not stopped by this lock alone.

## Management socket

The owner-UID Unix socket implements global management `version=1`; enrollment is absent from MCP. Every request supplies the same instance/candidate/epoch binding as HTTP. `inspect` takes an agent ID and returns canonical name, credential generation and a non-secret fingerprint. `claim`/`recover` take request ID, agent ID, expected credential generation and new credential, distinguish null-token claims from recovery and record accepted CAS/audit atomically. Identical retries return the same non-secret receipt; conflicting contents are rejected. `request_status` returns the recorded receipt.

Historical enrollment records remain intact; global receipts/audit use separate tables. Client CLI/profile recovery in a dedicated HOME is subsequent PR4a work.

## Unsupported capabilities

S1 publishes seven tools: health, ensure, register, whois, inbox, retire and unretire. The other 18 tools are listed in the fixture's `unimplemented` array and remain S2 work: sending/replying, read/ack/search, contacts, summaries/topics, reservations/macros, archive/attachments/Git writers, global signal writing/clearing and generated Git guard integration. No resources are published. Retained legacy non-published bodies do not become new public requirements.

The sequence is S1 → 4a → S2 → 4b → 4c. S1 does not complete global operation or installed cutover. Test legacy regression separately from the seven real HTTP capabilities and management socket.
