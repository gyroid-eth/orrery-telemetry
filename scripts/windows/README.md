# Windows lane (community-maintained)

Native Windows is **not a supported platform** of ORRERY Telemetry (README
"対応環境" / "Supported environments"); WSL2 is the Windows path the maintainer
verifies. This directory exists so that people who want native Windows tooling
can contribute it without touching the macOS install, launchers, hooks or test
suite.

- Put Windows-only scripts here (`scripts/windows/`), Windows-only tests under
  `tests/windows/`, and Windows-only guides as `docs/windows-*.md`.
- Nothing here is executed by `scripts/install.sh`, the hooks, or the dashboard
  on macOS. Shared code may carry `sys.platform == "win32"` branches only when
  the non-Windows path is byte-for-byte unchanged and the Windows CI job covers
  the branch.
- Rules and the review checklist: `CONTRIBUTING.md` → "Windows contributions".
