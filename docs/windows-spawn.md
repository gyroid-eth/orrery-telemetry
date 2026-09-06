Community-maintained / experimental. Validation: Windows 11 build 26200, CPython 3.12.10, PowerShell, Chrome.

# Native Windows launch boundary

On native Windows, opening NEW AGENT shows that spawn is not
supported and that WSL2 is the primary Windows path. The SPAWN button remains
disabled. The dialog remains available so users can read the reason.

`GET /api/spawn-names` returns HTTP 200 with the `unavailable` explanation plus
`names` (name, portrait, occupancy status), `adjectives`, and `naming`.
Windows reads these from the existing launcher array and scientist JSON using
Python, without starting Bash. It is a vocabulary-only response: directories,
providers and models are not advertised as usable launch options. The existing
dialog deliberately continues to show its unavailable state, not a selectable
roster. Native launch has not been implemented.

The reader accepts the current single literal ASCII-word adjective array, with
LF or CRLF, and uses `AGENTSTACK_SCIENTISTS_JSON` when nonempty, otherwise the
checkout's `dashboard/scientist_portraits.json`. JSON must be an object; keys
are sorted and filtered with the launcher's ASCII alphabetic rule. Shell
expressions, quoted array entries, empty vocabulary, duplicate adjectives and
missing/malformed files fail closed. This is not a general Bash interpreter;
changes to the source format must be reviewed with the Windows reader.

Catalog read failures return HTTP 503. Name suggestions use the same reader
and fail closed on unavailable data. Direct spawn requests on Windows return
an error before registration or launcher work, so better vocabulary access
cannot activate the unsupported spawn path.

The existing non-Windows catalog path is unchanged. Native catalog retrieval
and native launch are separate work under issue #9; this change only implements
vocabulary reading, does not change the support table, and does not validate
Codex App Bridge.

Validation covers the real HTTP handler without subprocess execution, the modal
unavailable-state handler and stale-response guard, canonical vocabulary hashes,
CRLF/Unicode paths, JSON overrides, malformed inputs, occupancy/suggestions,
direct spawn rejection, and a Chrome check of the NEW AGENT dialog.
The Windows suite does not establish full native support.
