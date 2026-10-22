# Native catalog and effort capabilities

> 日本語: [managed-catalog.md](managed-catalog.md)

## Current combinations

Read the [provider, model and harness definitions](getting-started.en.md#provider-model-harness), then choose a route from the table below. The four API combinations can become selectable once configured. This does not confirm that they work with your credentials, endpoint and model.

| Provider ID / connection | Harness | Models and preparation |
| --- | --- | --- |
| `claude` / existing Claude Code route | Claude Code (`claude-code`) | Uses the CLI's login and existing model catalog. This does not configure a new, general-purpose Anthropic API connection |
| `codex` / existing Codex CLI route | Codex CLI (`codex-cli`) | Uses the CLI's login, account access and model catalog for the installed version |
| `deepseek` / configured DeepSeek backend | `dsh` | Requires the pinned DeepSeek adapter, a credential reference and exact model IDs configured for the backend |
| `deepseek` / configured DeepSeek backend | `hermes` | Requires an explicitly configured profile and credential reference. Supported official DeepSeek `/anthropic` routes are mapped to `/v1` Chat Completions while preserving thinking |
| `strata` / displayed as Local | `dsh` | Requires an API compatible with the existing adapter, a credential reference and exact model IDs configured for the backend. Compatibility with arbitrary Local servers is not guaranteed |
| `strata` / displayed as Local | `hermes` | Requires an explicitly configured Chat Completions or Anthropic Messages route. Listing manual effort levels is separate from confirming that a real Local server accepts and applies them |

Real-machine checks of the four API combinations have so far been carried out on macOS in `legacy` mode. This does not mean every combination has been verified end to end; see [current limitations](#current-limitations). The additional static checks for Ubuntu WSL are described below. Main and helper models use the exact IDs in the same backend's `models` list, or its single `model` when no list is supplied. Availability is checked for each selected model × harness pair. Credentials from another provider and unconfigured models are never inferred.

[Codex App Bridge](codex-app.en.md) and [Antigravity / Gemini](antigravity.en.md) are separate, optional integrations. Their presence does not establish additional combinations with dsh / Hermes or allow an unsupported API to be used as `claude` / `codex`.

Choose optional runtime installation with `install.sh --enable-dsh --dsh-node /absolute/node` / `--enable-hermes`. See [installation and removal](install.en.md).

## Catalog and startup

For Ubuntu WSL, Linux catalog and launcher checks accept explicitly configured Secret Service references. A single `backends.<id>.credential_ref` supplies the credential reference to both harnesses and the balance reader. See the [reference requirements](install.en.md#ubuntu-wsl-secret-service-references-real-machine-acceptance-pending). Setup, runtimes, authentication, Mail, exit and resume for these API routes have not yet been verified end to end on real WSL. These checks do not establish general Linux compatibility.

`dashboard.managed_catalog.standard_catalog(config_file, *, mode)` returns choices for standard spawning. It uses the same static checks and configuration generation as `hooks.harness_child.selection()`, returning availability and fixed reason codes for each model. It does not retrieve keys, register agents or contact provider APIs. The common launcher checks the selection again at startup.

The API provider IDs are `deepseek` / `strata`, and the harness IDs are `dsh` / `hermes`. Main and helper choices use exact model IDs from the same backend in the manifest, subject to the generator's capabilities. Without explicit enablement, the catalog returns an empty array. Modes other than `legacy` report `unsupported_mode`; platforms other than macOS / Linux report `unsupported_platform`. Linux requires an explicit Secret Service reference and a reader and runtime that pass static preparation checks. Public catalog output excludes private URLs, account selectors and executable paths.

Appearing in the catalog does not confirm successful startup or work. In Telemetry's bundled **NEW AGENT**, you can select a DeepSeek / Local model and Harness (dsh or Hermes) marked available in the catalog. If you have also installed cockpit, follow the [end-to-end New Agent / Harness / Lineage and Mail check](getting-started.en.md#5-play-shiritori-as-an-end-to-end-check). For a launch without the screen, follow the provider, model, harness, parent and authentication requirements in the [API reference](api.en.md).

## Effort capabilities and defaults

Handwritten `efforts` / `default_effort` fields in a manifest do not establish capability. Each selected model's harness entry carries `efforts`, `default_effort` and `effort_source`; the picker and validation of explicit POST requests use that same entry.

In both the bundled **NEW AGENT** and cockpit's **New Agent**, leaving the effort control untouched or choosing **Use default** sends the specific default reported by the catalog, if it is valid. When that default is `null`, unknown or outside the available choices, the screen omits effort. It does not treat the first choice as the default. Omitting effort from an API request preserves the harness settings. The provider-level array exists only for compatibility and reflects the default available harness.

**dsh**: During installation or update, the pinned Node and runtime use the local `resolveModelInfo` to obtain levels and defaults for models in the saved or explicitly supplied manifest. This uses no keys, profiles, discovery or model API calls. Results are stored in that runtime generation's `evidence/effort-capabilities.json`. A cache for a changed runtime or model, or a failed extraction, yields unknown metadata (`null` / `null` / `unknown`). Effort choices become unavailable, but a launch that omits effort can still proceed. Extraction and catalog projection are skipped when dsh is disabled.

dsh reports `source=harness`; this is not a report from the Strata server. Its pinned adapter supplies four levels automatically. Strata's public source maps `max` to the same tier as `high`, but that source has not been checked against the deployed version.

**Hermes**: The pinned native source is read statically as data, and capabilities are reported for the effective route. Chat Completions uses `none`, `minimal`, `low`, `medium`, `high`, `xhigh` and `max`. For applicable models, the Anthropic Messages route with manual thinking uses `low`, `medium`, `high` and `xhigh`. These four manual levels do not apply to the separate Claude, Haiku or Kimi policies. Metadata becomes unknown if the relevant native structure changes or cannot be read. See [per-model capabilities](native-capabilities.md).

Hermes reports `default_effort=null`. Leaving effort unspecified or choosing **Use default** sends no `--reasoning` argument, preserving saved global or model-specific settings and the harness defaults. The screen does not guess a particular level. Unlike Chat Completions, `none` on the manual route does not explicitly disable thinking, so it is not offered there.

Experimental `low` / `high` settings left in older manifests or private backend files require migration of the user's configuration. The installer does not rewrite private settings without approval. Standard startup with effort omitted does not advertise those settings as harness defaults.

## Current limitations

- **Manual setup**: The [English example README](../examples/native-harnesses/README.en.md) ([Japanese](../examples/native-harnesses/README.md)) describes inactive examples with runtime references already filled in (workflow B), and help from an existing agent with non-secret settings (workflow A). After copying, check the references between files; you must enter the secret yourself and approve activation. The full set of DeepSeek / Local × dsh / Hermes combinations on Mac / WSL has not yet been verified end to end with real credentials and the standard examples. There is no common provider login CLI.
- **OS and screens**: The API spawn catalog is limited to `legacy` mode. Static checks cover macOS and explicitly configured Linux Secret Service references, but these API routes remain unverified end to end on real WSL. Existing WSL2 instructions for Claude Code / Codex CLI, and choices displayed in the bundled NEW AGENT, do not establish that the API routes work.
- **Local effort**: Hermes's four manual levels describe its ability to build a request. A real Local server has not yet been verified to accept and apply these levels. The pinned version sets temperature to 1 and raises the output limit to at least `budget+4096`, which may cause rejection under the server's context constraints. A conversation continuing through fallback does not prove that the selected effort took effect.
- **Local connection requirements**: Configurations that store and reference no key are not currently supported. dsh needs an HTTP/HTTPS API compatible with its fixed adapter; Hermes requires HTTPS and a supported API format. A keyless, HTTP-only server is not automatically usable with every harness. See the [setup requirements](native-setup.en.md#1-choose-a-provider-and-a-harness).
- **Memory and resume**: Each new Hermes child uses a stable, separate HOME for its project and canonical agent name. Standard resume checks the saved HOME pin and exact native session ID, then reuses that HOME. Old shared-HOME pins stop with `native_resume_legacy_profile`; old data and pins are not migrated automatically. Checks using the real CLI with a synthetic provider confirmed native storage, restoration with the same HOME / ID, and separation between children. Resume does not reinject the latest memory into the initial system prompt. End-to-end checks with real providers and the full Mail workflow remain incomplete. Resuming conversation history and verifying memory isolation and restoration are separate checks.
- **Telegram**: A common Telegram entry point for Hermes is still being designed. It is not an operational ORRERY entry point.
