# Native context and helper models

Thin dsh/Hermes launch uses the existing agent `ctx_used` / `ctx_window` fields
and existing spawn catalog `models[].harnesses[].helper_models` shape. There is
no additional provider endpoint or credential lookup in these readers.

## Coordination instructions

Native dsh/Hermes children receive concise, harness-neutral instructions from
`bin/lib/harness_instructions.md`. Startup, Mail notifications and common
failure rules use bounded includes from the existing shared template. Missing
or ambiguous boundaries stop rendering instead of silently omitting rules.
The native document covers shared-edit reservations, private scratch, waiting
and stop conditions without ORRERY child-creation or raw recovery procedures.
This changes the document, not dsh tool-subagent, Hermes auxiliary/delegation
settings or skill distribution. Existing Claude/Codex blocks and user
instruction files are unchanged. The child's actual program and model come
from its canonical launch task or runtime binding.

## Activity classification boundary

API 24 observes native activity from the same captured pane as context.
`dashboard.harness_activity.parse_activity(text, program)` returns `work`,
`question`, `ask` or `wait` only from supported current controls. dsh questions
and approvals need both their panel title and operation hint near the bottom.
Hermes instead pairs the current modal prompt (`⚠` for approval, `?`/`✎` for
questions) with the matching operation hint. A long command can push its title
offscreen without hiding those controls; that still reports `ask`/`question`.
The prompt icon alone or hint alone is insufficient.
dsh hides the ordinary composer during a modal. A visible ordinary input frame
therefore identifies panel-like text above it as transcript, not a current
modal. Without that contradictory frame, a modal takes priority over the generic
interrupt hint. A dsh operation hint with a missing title stays unknown.
The context parser itself does not infer activity.

Supported formats are dsh-tui 0.16.1's English/Chinese approval and question
panels (including folded questions), its interrupt footer, and Hermes
d923a5eb's bundled English/Japanese approval and clarify panels. Hermes busy
and idle classification uses the input frame and prompt symbols, independent
of the profile/model name and translated placeholder text.
Missing, hidden, incomplete, offscreen or unsupported localized controls stay
`act_state: null`. Each new capture replaces the previous observation; a missing
control does not preserve the old activity. Display text is not authenticated
telemetry and can be imitated by terminal output.

Idle is not inferred from absence alone. `wait` needs a complete current input
frame and no work/question/approval evidence. Hermes uses matching rules around
the input and a normal `❯` prompt (with an optional profile name); working uses
the `☤` state icon instead. dsh uses its complete `⌸ ❯ … ⛶` input box and current
context footer, without the interrupt hint. Broken/truncated frames, startup
consent screens, hidden chrome and unsupported custom prompt shapes stay null.
Multiline/wrapped input outside these complete shapes also stays unknown.
Null means unknown, not idle, finished or ready. Activity never changes process
liveness or stop eligibility.
The existing Claude/Codex classification is unchanged. No new watcher, native
patch, status file or provider call is introduced.
These additions apply to legacy agent rows and graph nodes; the global runtime
observation route is unchanged.

The input shapes are observed in real idle/work/resume pane captures and
defined by Hermes `cli_tui_mixin.py:272–346,2143–2161` and dsh
`components/PromptInput.js:59,3826–3841` / `screens/StatusLine.js:370–376`.
Tests keep unmodified footer excerpts, excluding the surrounding private
transcript. Hermes questions, short approvals and long approvals (24- and
60-row panes) also use real capture footer fixtures. dsh question/approval
controls are checked against pinned source formatters; real dsh modal captures
are not part of this evidence.

## Context

`dashboard.harness_context.parse_context(text, program)` receives an already
captured pane and the actual runtime program (`dsh` or `hermes`). It observes
the last twelve lines only and returns context percentage and display window.
It does not determine process lifetime, activity, model identity, billing or
exact token counts. Hidden or truncated status returns nulls, never zero usage
or a window guessed from a model name. The normal Claude/Codex parser remains
separate. Callers must also avoid applying its model-name window fallback to
native unknown results.

Source formats at the pinned runtime versions:

- dsh 0.2.1-rc.2 / dsh-tui 0.16.1: `tuiDisplayPrefs.js:72`
  `formatContextUsage`, rendered by `screens/StatusLine.js:161–197`:
  `ctx 19% (24k/128k)` or `ctx 24k/128k (19%)`. This is the TUI's current
  occupancy reading, not lifetime input/output usage. dsh's fractional reading
  is rounded to the existing integer percentage; over-capacity stays visible.
- Hermes d923a5eb: `hermes_cli/cli_status_bar_mixin.py:1017–1129`, the native
  wide status row: `☤ Qwen │ ~24K/128K │ [██░░░░░░░░] ~19%`. Narrow/medium
  rows without a complete capacity reading remain unknown. Native `~` readings
  are estimates; preserving a visible percentage does not make them exact.

These are observations of a terminal display, just like the existing native
providers, not authenticated telemetry. Localized/custom/hover displays without
the supported footer shape can remain unknown. Old offscreen transcript text
does not supply context. No extra watcher, session file or model API is used.

## Model and helper catalog

The operator-owned manifest declares `backends[backend].models` as formal model
ID strings. If absent, its single `model` is the only configured ID. Main and
helper selections share this same backend set. Populating a live manifest is
an operator deployment step, not catalog discovery.

`dashboard.harness_helpers.model_rows(manifest, backend_id, *, selections,
statuses, product_root)` projects public model rows. The internal maps use
`(model, harness)` keys: successful shared `hooks.harness_child.selection()`
results and that exact pair's availability/I5 fields. The catalog caller checks
each pair independently; it must not copy the primary model's effort metadata
to other models. Missing/unavailable pairs expose unknown effort metadata and
only `same` as helper. The shared selection cache owns subprocess budgets.

### Hermes の manual Anthropic 経路の Effort

モデルとハーネスごとの能力として、固定版 Hermes の manual thinking 経路では
`low`・`medium`・`high`・`xhigh` を公開する。native の budget 定義と CLI 語彙を
読み、budget の小さい順に並べる。選択中の正式モデル ID で判定し、profile の
主モデルから能力をコピーしない。Claude・Haiku・Kimi の別の thinking 方針には
この4段を当てはめない。関係する native の構造が変わった場合は不明とする。

未操作と「Use default」は `default_effort=null` のまま、`--reasoning` を渡さない。
利用者が保存した全体・モデル別の設定を維持する。明示した段だけがその起動に
優先する。既存の Chat 経路の7段と起動引数の組立は変更しない。
manual で専用 budget のない `minimal`・`max`・`ultra` は独立の段にしない。
`none` はこの経路では thinking の省略であり、明示的な無効化として公開しない。

これはハーネスが要求を組み立てる能力であり、Local サーバの受理・効果の保証ではない。
固定版の manual は temperature を1とし、max_tokens を budget+4096 以上に上げる。
context の制約によっては拒否され得るため、実サーバの受け入れは別に確認する。
要求を拒否された後に native が fallback して会話できたことを、選んだ Effort が
有効だった証拠にしない。

Alternate helpers require literal `HELPER_MODEL_CONTRACT = 1` in the selected
`integrations/<harness>/thin_config.py`. The catalog reads this via AST and
does not import the generator. The helper list is the intersection of configured
same-backend IDs, public model IDs and successful selections on that harness.
Old or unavailable generators expose `same` only. Public output contains only
model IDs/labels and capability fields, never runner paths or configuration.

The catalog's `validate_helper_model(value, choices)` is also the POST
validator: omitted means `same`, foreign/undeclared IDs raise
`invalid_helper_model`. The common launcher carries explicit `--helper-model`
into generator INPUT. Each generator validates the same backend set again.
No second provider or credentials are inferred. dsh projects its subagent model;
Hermes projects auxiliary task and delegation models. Exact generator mappings
belong to the shared thin-generator contract and their implementations.

The New Agent screen folds the selected main ID into `Same as main` for display, so a
single-model backend does not gain a redundant helper control. This does not
remove its formal ID from the backend's accepted set.
