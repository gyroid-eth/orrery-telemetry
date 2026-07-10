#!/usr/bin/env python3
"""Post-clone patch for the upstream agent-mail git-walk event-loop hang.

The upstream ``mcp_agent_mail`` server evaluates file-reservation staleness by,
for every file a reservation's glob matches, running a paths-filtered
``git.iter_commits(...)`` — a full repository-history walk — synchronously on the
asyncio event loop. On a large working tree under frequent commits (e.g. an
Obsidian vault with an hourly backup cron and thousands of commits), a single
broad reservation such as ``runs/*refine*/**`` expands to hundreds of files and
turns into files x full-history walks, freezing the whole server for minutes.
Every MCP call then times out until a watchdog restarts the process, which
re-hangs on the same reservation.

This patch hardens ``src/mcp_agent_mail/app.py`` with defense in depth:
  1. Cap glob expansion (``_MAX_GLOB_MATCHES``) so the filesystem walk is bounded.
  2. Skip git-history probing entirely above ``_MAX_GIT_ACTIVITY_MATCHES`` matches
     (staleness falls back to filesystem mtime + mail activity; semantics preserved).
  3. Replace the per-file ``iter_commits`` loop with a single batched
     ``git log -1 --format=%ct -- <paths...>`` (one walk that stops at the first
     matching commit).
  4. Run the whole glob+git probe off the event loop via ``asyncio.to_thread``
     with a hard ``_GIT_ACTIVITY_TIMEOUT_SECONDS`` timeout, so no reservation can
     ever stall async request handling.

Idempotent: re-running is a no-op once the marker constant is present. Fails
gracefully (non-zero exit + message) if the upstream anchors have drifted, so the
installer can warn and continue rather than corrupt the file.

Usage: python3 agent-mail-git-walk-hang.py /path/to/mcp_agent_mail
"""
import sys
from pathlib import Path

REPL = [
    # 1. guard constants
    (
        '_GLOB_MARKERS: tuple[str, ...] = ("*", "?", "[")\n\n'
        "# Virtual namespace prefixes for non-filesystem reservations (bd-14z)",
        '_GLOB_MARKERS: tuple[str, ...] = ("*", "?", "[")\n\n'
        "# Guards against event-loop stalls when evaluating file-reservation staleness\n"
        "# on large workspaces (many files x deep git history). Without these, a broad\n"
        "# glob like ``runs/*refine*/**`` expands to hundreds of files and triggers a\n"
        "# full-history ``git log`` walk per file, synchronously on the event loop.\n"
        "#   - _MAX_GLOB_MATCHES: cap ``**`` glob expansion so the filesystem walk is bounded.\n"
        "#   - _MAX_GIT_ACTIVITY_MATCHES: above this many matches, skip git-history probing\n"
        "#     entirely and fall back to filesystem mtime + mail activity (semantics preserved).\n"
        "#   - _GIT_ACTIVITY_TIMEOUT_SECONDS: hard wall-clock cap for the off-loop git probe.\n"
        "_MAX_GLOB_MATCHES: int = 500\n"
        "_MAX_GIT_ACTIVITY_MATCHES: int = 20\n"
        "_GIT_ACTIVITY_TIMEOUT_SECONDS: float = 3.0\n\n"
        "# Virtual namespace prefixes for non-filesystem reservations (bd-14z)",
    ),
    # 2. glob cap
    (
        "    if _contains_glob(normalized):\n"
        "        return list(base.glob(normalized))\n"
        "    candidate = base / normalized",
        "    if _contains_glob(normalized):\n"
        "        # Bound the walk: a ``**`` pattern on a huge working tree would otherwise\n"
        "        # enumerate the entire subtree. islice stops the generator early so we\n"
        "        # never materialize (or stat) more than the cap.\n"
        "        from itertools import islice\n\n"
        "        return list(islice(base.glob(normalized), _MAX_GLOB_MATCHES))\n"
        "    candidate = base / normalized",
    ),
    # 3. _latest_git_activity rewrite + off-loop probe helper
    (
        "def _latest_git_activity(repo: Optional[Repo], matches: Sequence[Path]) -> Optional[datetime]:\n"
        "    if repo is None:\n"
        "        return None\n"
        "    repo_root = Path(repo.working_tree_dir or \"\").resolve()\n"
        "    commit_times: list[datetime] = []\n"
        "    for match in matches:\n"
        "        try:\n"
        "            rel_path = match.resolve().relative_to(repo_root)\n"
        "        except Exception:\n"
        "            continue\n"
        "        try:\n"
        "            commit = next(repo.iter_commits(paths=str(rel_path), max_count=1))\n"
        "        except StopIteration:\n"
        "            continue\n"
        "        except Exception:\n"
        "            continue\n"
        "        commit_times.append(datetime.fromtimestamp(commit.committed_date, tz=timezone.utc))\n"
        "    if not commit_times:\n"
        "        return None\n"
        "    return max(commit_times)",
        "def _latest_git_activity(repo: Optional[Repo], matches: Sequence[Path]) -> Optional[datetime]:\n"
        '    """Most-recent commit timestamp touching any of ``matches``.\n\n'
        "    Bounded so it can never stall the event loop:\n"
        "      * Skips entirely when there are more than ``_MAX_GIT_ACTIVITY_MATCHES``\n"
        "        matches (staleness naturally falls back to fs mtime + mail activity).\n"
        "      * Uses a single batched ``git log -1`` over all paths instead of one\n"
        "        full-history ``iter_commits`` walk per file.\n"
        "    Callers should run this off the event loop (see ``asyncio.to_thread`` in\n"
        "    ``_collect_file_reservation_statuses``); it may block for a git subprocess.\n"
        '    """\n'
        "    if repo is None:\n"
        "        return None\n"
        "    # Too many matches → a broad reservation whose git history is not worth\n"
        "    # (and too expensive to) probe. Fall back to filesystem/mail signals.\n"
        "    if len(matches) > _MAX_GIT_ACTIVITY_MATCHES:\n"
        "        return None\n"
        "    repo_root = Path(repo.working_tree_dir or \"\").resolve()\n"
        "    rel_paths: list[str] = []\n"
        "    for match in matches:\n"
        "        try:\n"
        "            rel_paths.append(str(match.resolve().relative_to(repo_root)))\n"
        "        except Exception:\n"
        "            continue\n"
        "    if not rel_paths:\n"
        "        return None\n"
        "    try:\n"
        "        # One walk that stops at the first commit touching any of the paths.\n"
        '        raw = repo.git.log("-1", "--format=%ct", "--", *rel_paths)\n'
        "    except Exception:\n"
        "        return None\n"
        '    raw = (raw or "").strip()\n'
        "    if not raw:\n"
        "        return None\n"
        "    try:\n"
        "        return datetime.fromtimestamp(int(raw.splitlines()[0]), tz=timezone.utc)\n"
        "    except (ValueError, OSError, OverflowError):\n"
        "        return None\n\n\n"
        "def _reservation_activity_probe(\n"
        "    workspace: Optional[Path], repo: Optional[Repo], pattern: str\n"
        ") -> tuple[list[Path], Optional[datetime], Optional[datetime]]:\n"
        '    """Blocking glob + filesystem-mtime + git probe for a single reservation.\n\n'
        "    Returns ``(matches, fs_activity, git_activity)``. Designed to be run off the\n"
        "    event loop via ``asyncio.to_thread`` so a slow filesystem walk or git\n"
        "    subprocess can never stall async request handling.\n"
        '    """\n'
        "    if workspace is None:\n"
        "        return [], None, None\n"
        "    matches = _collect_matching_paths(workspace, pattern)\n"
        "    if not matches:\n"
        "        return [], None, None\n"
        "    fs_activity = _latest_filesystem_activity(matches)\n"
        "    git_activity = _latest_git_activity(repo, matches)\n"
        "    return matches, fs_activity, git_activity",
    ),
    # 4. off-loop probe in the async status loop
    (
        "            if workspace is not None:\n"
        "                matches = _collect_matching_paths(workspace, reservation.path_pattern)\n"
        "                if matches:\n"
        "                    fs_activity = _latest_filesystem_activity(matches)\n"
        "                    git_activity = _latest_git_activity(repo, matches)",
        "            if workspace is not None:\n"
        "                # Run the (potentially slow) glob + git probe off the event loop\n"
        "                # with a hard timeout so one broad reservation cannot stall the\n"
        "                # whole server. On timeout/failure we degrade to no fs/git signal\n"
        "                # (staleness still uses agent inactivity + mail activity).\n"
        "                try:\n"
        "                    matches, fs_activity, git_activity = await asyncio.wait_for(\n"
        "                        asyncio.to_thread(\n"
        "                            _reservation_activity_probe, workspace, repo, reservation.path_pattern\n"
        "                        ),\n"
        "                        timeout=_GIT_ACTIVITY_TIMEOUT_SECONDS,\n"
        "                    )\n"
        "                except (asyncio.TimeoutError, Exception):\n"
        "                    matches, fs_activity, git_activity = [], None, None",
    ),
]

MARKER = "_MAX_GIT_ACTIVITY_MATCHES: int = 20"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: agent-mail-git-walk-hang.py /path/to/mcp_agent_mail", file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    app = root / "src" / "mcp_agent_mail" / "app.py"
    if not app.exists():
        print(f"skip: {app} not found (unexpected agent-mail layout)", file=sys.stderr)
        return 2
    text = app.read_text()
    if MARKER in text:
        print(f"already patched: {app}")
        return 0
    for i, (old, new) in enumerate(REPL, 1):
        n = text.count(old)
        if n != 1:
            print(
                f"skip: upstream anchor {i} not uniquely found ({n}x) in {app}; "
                "upstream may have changed — leaving file untouched",
                file=sys.stderr,
            )
            return 3
        text = text.replace(old, new)
    app.write_text(text)
    print(f"patched: {app} (agent-mail git-walk hang fix)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
