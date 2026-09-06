Community-maintained / experimental. Validation: Windows 11 build 26200, CPython 3.12.10, PowerShell, Chrome.

# Native Windows launch boundary

On native Windows, opening NEW AGENT shows that launch catalog / spawn is not
supported and that WSL2 is the primary Windows path. The SPAWN button remains
disabled. The dialog remains available so users can read the reason.

`GET /api/spawn-names` returns HTTP 200 with an `unavailable` explanation,
without starting Bash or waiting for the previous three-second timeout.
This is an explicit unavailable state, not a working launch catalog.

The existing non-Windows catalog path is unchanged. Native catalog retrieval
and native launch remain separate future work under issue #9; this change does
not implement either, change the support table, or validate Codex App Bridge.

Validation covers the real HTTP handler without subprocess execution, the modal
unavailable-state handler and stale-response guard, and a Chrome check of the
NEW AGENT dialog. The Windows suite does not establish full native support.
