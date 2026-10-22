# Inactive configuration examples for dsh / Hermes

> 日本語: [README.md](README.md)

**Adding DeepSeek or Local for the first time?** Follow the [setup guide](https://github.com/gyroid-eth/orrery-telemetry/blob/master/docs/native-setup.en.md) for harness installation and key creation/storage. This document explains how to copy and connect the example settings.

Example version: 1. Installation creates examples, not active backend settings or credentials.
Do not register an example manifest directly with the service.

## B: start with runtime references already filled in

After an optional harness installation, the installer validates its current/owned receipt and
publishes four JSON files under `resolved/<snapshot-id>/`. It reports that path after ownership
is committed. The manifest always has `enabled: false`. Without either runtime, only static
examples are provided. With one runtime, only that harness is referenced by the snapshot manifest;
the unused JSON still contains placeholders and must not be enabled as-is.

1. Choose the harness, backend, formal model ID and a new private destination directory.
2. An existing agent can help prepare non-secret settings from this README. Copy the reported
   snapshot into a **new separate directory**, with directory mode 0700 and JSON mode 0600.
   Keep existing settings; review proposed changes before editing them.
3. Adjust the copied manifest's `harnesses.dsh.config_file` / `harnesses.hermes.config_file`
   and the dsh config's `backend_file` to the copied files' **absolute paths**. Remove unused
   harness entries. JSON does not expand environment variables or placeholders.
4. Confirm the backend URL, model and non-secret credential selector. Never paste a key to an
   agent or put its value in JSON, environment variables or command arguments.
5. The person stores the secret using the OS's hidden input. Report only success/failure and
   the non-secret reference to the agent.
6. After the person approves activation, set **only the private copy** to `enabled: true`.
   Use the existing `hooks/harness_child.py validate --manifest <absolute-private-manifest>
   --harness <dsh|hermes> --backend deepseek --model <formal-id>` with the verified Python.
   Static reference validation does not verify credentials, connectivity or Mail delivery.
7. With approval, pass that reference to the existing installer's `--managed-spawn-config` option.
   This performs install/service work; an assisting agent must not run it without approval.
   Then separately accept standard New Agent startup, parent/child Mail, normal exit and exact
   resume under the same identity.

Both harnesses use four files and thirteen reference slots: ten runtime references and three
peer-file paths. These are installer/agent tasks, not thirteen manual entries by the person.
B saves runtime transcription; destination selection, secret input, activation and real acceptance remain.

## A: missing runtimes, model changes and adjustments

The static [manifest](manifest.example.json), [dsh config](dsh-config.example.json),
[dsh backends](dsh-backends.example.json) and [Hermes config](hermes-config.example.json)
are incomplete. `<ABS_...>` placeholders cannot be launched. An existing agent may help resolve
non-secret references from verified current/owner receipts and committed Hermes PM facts.
Do not guess generations or virtual environments, inspect private profiles/auth stores/memory,
or search broad runtime directories. Stop for installation repair when references are missing or inconsistent.

The static JSON uses the Mac credential format. For WSL, apply the substitutions below.
Main/helper models must use formal IDs in the same backend. Do not add trial effort, request-count,
lifetime or large context/output caps.

### Switching to Local / Strata

B's standard examples target DeepSeek. For Local, use backend ID `strata` and align these
fields in the private copy with the same destination. Keep runtime references and copied-file
absolute paths consistent.

| File | Fields to change |
| --- | --- |
| Manifest | Set `backends.strata.model` / `models` to formal model IDs confirmed by the person |
| dsh backends | Rename the entry to `backends.strata`; set `model` and `base_url` to the formal ID and the person's HTTPS URL |
| Hermes config | Rename the entry to `profiles.strata`; set `model`, `base_url` and `api_mode`. Use the server's supported `chat_completions` or `anthropic_messages` mode. Choose a `provider` name that does not collide with a builtin, such as `orrery-strata-manual` |
| OS credential reference | On Mac, point dsh's `backends.strata.keychain` and Hermes's `profiles.strata.key_cmd` to the same person-selected credential. On WSL, use only the manifest's `backends.strata.credential_ref` and remove the corresponding keychain/key_cmd fields |

Do not retain the DeepSeek URL, model or credential selector while treating the copy as Local.
Do not guess an endpoint path or API mode; use the server specification confirmed by the person.
This dsh configuration has no `api_mode` switch, so adding Hermes fields does not select the
same protocol. Remove unused DeepSeek entries from the copy and include the selected `--model`
in `backends.strata.models`.

With the verified Python, run `hooks/harness_child.py validate --manifest <absolute-private-manifest>
--harness <dsh|hermes> --backend strata --model <formal-id>`. This static check is separate from
real-machine acceptance of all four DeepSeek/Local × dsh/Hermes standard UI spawn combinations
on Mac/WSL. Secret input and the person's activation approval remain the same as above.

## Mac and WSL credential references

**Mac:** dsh uses the backend keychain service/account, while Hermes uses its profile key_cmd.
Both examples refer to the conversation selector `orrery-dsh-deepseek` / `api-key`. The person
registers it using hidden OS input, such as Keychain Access. The agent must not run a lookup or
ask to see the key. The existing Mac balance worker uses the separate `deepseek-api` service.
Conversation success proves neither balance retrieval nor that the account matches. Balance
acceptance is separate; these instructions do not require duplicate key registration.

**WSL / Linux Secret Service:** in a product containing #330 support, Linux snapshots put this
single reference in manifest.backends.deepseek and omit dsh's keychain and Hermes's key_cmd.
When preparing static A manually, delete those two old fields before applying this form:

```json
{
  "credential_ref": {
    "store": "secret-service",
    "attributes": {"service": "orrery-dsh-deepseek", "account": "api-key"}
  }
}
```

The person stores the secret under the same attributes. Secret Service, the D-Bus session,
unlocking and `/usr/bin/secret-tool` are OS preparation tasks. Do not have the agent run lookup
or the internal reader, and do not automate secret registration. Both harnesses, guard and quota
use the same internal reference. Combining it with old Mac credentials is an error; it does not
fall back to another store. Do not add the Secret Service reference on Mac.

Generating inactive examples does not test store availability, unlocking or secret retrieval.
Real Ubuntu WSL Node/PM, secret input, standard spawn/Mail/exit/exact resume are not yet accepted.
Copying a file into older software does not add WSL support. Static preparation for macOS legacy
and Linux legacy is not a guarantee for every OS or service mode.

## Updates, removal and evidence

Existing examples and snapshots are never overwritten. Updates add missing files or a new
content-derived snapshot. Keeping an old example does not confirm that it is current or usable.
Choose whether to retain it or move it aside and request a fresh copy. Pre-existing unrecorded
files are not adopted. Uninstall/purge removes only recorded, unchanged examples; edited files,
links, unrecorded files and private copies are retained. Activation and user-setting migration are separate.

The public endpoint candidates are `/anthropic` for dsh and `/v1` for Hermes Chat. Source-level
projection is separate from accepting all example bytes on the actual wire. The final dsh wire,
the person's secret input and fresh Mac/WSL end-to-end setup remain unverified. Synthetic
distribution/ownership tests cannot establish those results.
