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


### Prepare a DeepSeek snapshot without an agent

You can start with the installer's B snapshot without Claude Code or Codex.
First complete [steps 2–4 of the setup guide](../../docs/native-setup.en.md#2-install-the-harnesses):
install a harness and store your key in the OS. The copy below does not retrieve a key.

Run this from the Telemetry source folder. Replace `SNAPSHOT` with the absolute path after
`inactive snapshot (copy and finish setup):`, and `PYTHON` with the absolute Python executable
path after `python:` in the same installer's output. Choose a new `CONFIG_DIR`.
The copy stops if that destination already exists. Choose another location rather than
deleting or overwriting existing settings.

```bash
# Replace these two paths with the values printed by your installer.
SNAPSHOT='/absolute/path/to/examples/native-harnesses/resolved/snapshot-id'
PYTHON='/absolute/path/printed-after-python'
CONFIG_DIR="$HOME/.config/orrery/native-deepseek"

"$PYTHON" - "$SNAPSHOT" "$CONFIG_DIR" <<'PY'
import json
import os
from pathlib import Path
import sys

source = Path(sys.argv[1]).expanduser().resolve(strict=True)
target = Path(sys.argv[2]).expanduser().resolve()
names = (
    "manifest.example.json", "dsh-config.example.json",
    "dsh-backends.example.json", "hermes-config.example.json",
)
documents = {name: json.loads((source / name).read_text()) for name in names}
manifest = documents["manifest.example.json"]
if manifest.get("enabled") is not False or not manifest.get("harnesses"):
    raise SystemExit("STOP: choose an inactive installer-generated runtime snapshot")
for harness, entry in manifest["harnesses"].items():
    if harness not in ("dsh", "hermes"):
        raise SystemExit("STOP: unknown harness")
    entry["config_file"] = str(target / (harness + "-config.example.json"))
documents["dsh-config.example.json"]["backend_file"] = str(
    target / "dsh-backends.example.json"
)
os.umask(0o077)
target.mkdir(mode=0o700, parents=True, exist_ok=False)
for name, document in documents.items():
    with (target / name).open("x") as stream:
        json.dump(document, stream, indent=2)
        stream.write("\n")
print("COPIED_INACTIVE:", target / "manifest.example.json")
print("HARNESS:", ", ".join(manifest["harnesses"]))
PY
```

`COPIED_INACTIVE:` and `HARNESS:` confirm the copy; it is still disabled.
`FileExistsError` means the destination already exists. If the installer produced only static
examples, return to harness installation instead of copying them here.
With only one harness installed, use only the harness shown after `HARNESS:`; ignore the unused JSON.

Open the copied `manifest.example.json` in a text editor. For the standard DeepSeek model and
credential selector (`orrery-dsh-deepseek` / `api-key`), keep the supplied URLs and references.
After confirming OS key storage and your harness choice, change only the first
`"enabled": false` to `"enabled": true` and save. Never put the key itself in JSON.
For a different credential selector or a Local model, apply the changes below first.

In the same terminal, validate the harness you will use. For Hermes, replace
`--harness dsh` with `--harness hermes`. If you use both, validate each once.

```bash
"$PYTHON" hooks/harness_child.py validate \
  --manifest "$CONFIG_DIR/manifest.example.json" \
  --harness dsh --backend deepseek --model deepseek-flash \
  && printf 'CONFIG_CHECK_OK\n'
```

`CONFIG_CHECK_OK` means the configuration check passed; it does not check API access or key validity.
`harness_not_enabled` means the copy is still disabled; `model_mismatch` means the model is not
in the manifest's list. For `KeyError`, check that your harness appears after `HARNESS:` above.
For `runtime_not_prepared`, check harness installation and the snapshot's generation.
For another error, inspect its message without sharing a key and resolve it
before continuing.

![A real WSL terminal after copying the examples and passing dsh and Hermes configuration checks](../../docs/img/wsl-copy-static-validation.png)

[Copy and configuration-check GIF (10 seconds)](../../docs/img/wsl-copy-static-validation.gif).
This non-interactive example ran in a real WSL terminal, viewed in Chrome on a Mac. It holds three captured screens long enough to read; it is not a recording of manual typing or real-time progress.
The capture's `copy.py` file contains the copy code shown above. Extra lines such as `STAGE_*` are capture checks, not output from your commands. Run the code blocks above and look for `COPIED_INACTIVE`, `HARNESS` and `CONFIG_CHECK_OK`. The `/tmp/...` path is also specific to the capture.
**For your own setup, keep the validated copy set to `enabled: true` when continuing below.** The screen's final `FINAL_COPY_ENABLED=false` only cleans up the capture's temporary copy. No API connection, key retrieval or installation occurs.

After validation, run this once yourself from the same source folder. Review any proposed
changes and answer the yes/no prompts yourself. After `Install complete`, reload the browser
and [try one agent](../../docs/native-setup.en.md#6-try-one-agent-from-new-agent).

```bash
./scripts/install.sh --keep-mail --managed-spawn-config "$CONFIG_DIR/manifest.example.json"
```


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

<a id="dynamic-local-connections"></a>

#### Dynamic connections (recommended)

Use this once for each Local connection, including existing workstation/remote connections. Keep distinct IDs and user-chosen labels. In the private manifest, replace that connection's `model` / `models` fields with:

```json
"local-workstation": {
  "kind": "strata",
  "label": "Workstation",
  "model_source": "server",
  "urls": ["https://llm.example.com/v1", "https://second.example.com/v1", "https://third.example.com/v1"],
  "context_override": 64000
}
```

`urls` accepts one or more distinct configured API bases, with no two-URL/GPU limit. A base ending in `/v1` is used directly; otherwise `/v1` is appended. No credentials, query or fragment belong in a URL. Keep only URLs that use the same connection's credential. Use another connection ID for a different credential. Request concurrency is two and each HTTP request has a three-second timeout; more configured URLs can take longer to list.

Keep the same connection ID in the dsh backend map and Hermes profile map. For dynamic mode, dsh needs its existing `keychain` selector, or the manifest's Linux `credential_ref`; its old `model`, `model_definition` and `base_url` are no longer authoritative and can be removed. Hermes keeps its non-builtin `provider` name and existing credential reference; remove the old per-model `model`, `context_length`, `base_url` and `api_mode` from that profile. The manifest URL supplies Hermes's `chat_completions` route. No second model list or URL update is needed in these files. Runtime receipts, paths and key protections are unchanged. Mac model discovery accepts the canonical `/usr/bin/security find-generic-password -s SERVICE -a ACCOUNT -w` reference; it does not run arbitrary `key_cmd` helpers.

The optional `context_override` is a fallback when the same selected model row has no valid `meta.n_ctx`; otherwise that row's reported context wins. Without either, the common default is64,000 tokens. The picker labels `server`, `override` or `default`; these last two are settings, not measurements. dsh's generated output limit defaults to8,192 tokens (bounded by context). Effort is unreported and omitted, and helper stays Same as main. A valid `data[].id` is selectable with absent or unknown status; only `status.value: unloaded` disables it. IDs, not labels, are sent to the native harness. Equal IDs on different connections or URL numbers remain distinct.

For static checks, use the verified Python with `hooks/harness_child.py validate --manifest <private-manifest> --harness <dsh|hermes> --backend <connection-id> --url-index <zero-based-index> --model <id>`. This makes no request and reads no key. Use the normal reviewed `--keep-mail --managed-spawn-config <absolute-manifest>` install procedure once, then reload both NEW AGENT screens. They fetch models using the existing key reference. Each actual launch/exact resume performs one selected-URL `/v1/models` lookup; a missing ID, explicit unloaded status or failure refuses rather than choosing another model. The catalog's `url_key` binds the selected configured URL through POST/launcher; a changed/reordered URL requires a fresh selection. Exact resume retains the original connection/URL/model; old fixed sessions need a manual decision if that connection was migrated. Keep the old copy for ordinary manual rollback.

Model-list examples from primary documentation (URLs illustrate server setup, not universal harness compatibility):

- [llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md): with an explicit `--port 8080`, the API base is `http://localhost:8080/v1`.
- [LM Studio](https://lmstudio.ai/docs/developer/openai-compat/models): `http://localhost:1234/v1`; JIT loading can list downloaded models, so absence of status is not a loaded claim.
- [vLLM](https://docs.vllm.ai/en/latest/getting_started/quickstart/): `http://localhost:8000/v1`; configure authentication on the server as required by your deployment.
- [Ollama](https://docs.ollama.com/api/openai-compatibility): `http://localhost:11434/v1`; its local API ignores the client's key, while ORRERY's existing key-reference requirement remains.

Discovery accepts HTTP/HTTPS. dsh still uses its existing DeepSeek Messages adapter and needs a compatible Messages endpoint; a model-list response alone does not prove that compatibility. Hermes still requires HTTPS and uses OpenAI chat completions in dynamic mode. An HTTP-only example therefore is not directly usable with Hermes. This change adds no transport adapter, TLS exception or model-control operation. The final observation does not prevent a subsequent model change or detect a server that ignores the selected ID.

#### Existing fixed-model connections (compatibility)

To keep an existing Local server and add another, follow [multiple connections](../../docs/configuration.en.md#native-connections). Add the same new connection ID to the manifest, dsh backend map, and Hermes profile map instead of replacing the existing connection.

B's standard examples target DeepSeek. For Local, use backend ID `strata` and align these
fields in the private copy with the same destination. Keep runtime references and copied-file
absolute paths consistent.

| File | Fields to change |
| --- | --- |
| Manifest | Set `backends.strata.model` / `models` to formal model IDs confirmed by the person |
| dsh backends | Rename the entry to `backends.strata`; set `model` and `base_url` to the formal ID and a compatible HTTP/HTTPS API URL |
| Hermes config | Rename the entry to `profiles.strata`; set `model`, `base_url` and `api_mode`. HTTPS is required. Use the server's supported `chat_completions` or `anthropic_messages` mode. Choose a `provider` name that does not collide with a builtin, such as `orrery-strata-manual` |
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
