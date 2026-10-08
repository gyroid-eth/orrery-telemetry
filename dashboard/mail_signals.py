"""Read-only signal inventory and opt-in pruning; never opens the Mail DB."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import os
import re
from pathlib import Path
import stat
import subprocess
import time

# A recently stopped recipient can resume and still receive its notification.
ORPHAN_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Signal:
    path: Path
    agent: str
    info: os.stat_result


def session_names() -> set[str] | None:
    """Unknown is not absent: tool errors/timeouts must retain backlog."""
    try:
        result = subprocess.run(['tmux', 'list-sessions', '-F', '#{session_name}'],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return set(result.stdout.splitlines())
    diagnostic = result.stderr.strip()
    if result.returncode == 1 and (diagnostic.startswith('no server running on ')
                                   or diagnostic == 'no sessions'
                                   or re.fullmatch(r'error connecting to .+ \(No such file or directory\)', diagnostic)):
        return set()
    return None


def inventory(agent_dirs: list[str]) -> list[Signal]:
    """Both per-message and legacy layouts, without following symlinks."""
    signals = []
    for root in agent_dirs:
        try:
            with os.scandir(root) as agents:
                for agent in agents:
                    try:
                        if agent.is_file(follow_symlinks=False) and agent.name.endswith('.signal'):
                            signals.append(Signal(Path(agent.path), agent.name[:-7], agent.stat(follow_symlinks=False)))
                        elif agent.is_dir(follow_symlinks=False):
                            with os.scandir(agent.path) as files:
                                for file in files:
                                    try:
                                        if file.name.endswith('.signal') and file.is_file(follow_symlinks=False):
                                            signals.append(Signal(Path(file.path), agent.name, file.stat(follow_symlinks=False)))
                                    except OSError:
                                        continue
                    except OSError:
                        # A successful delivery can remove a recipient mid-scan.
                        continue
        except OSError:
            continue
    return signals


def is_orphan(signal: Signal, sessions: set[str] | None, runtime_dir: Path, now: float) -> bool:
    # Even a broken headless manifest belongs to the headless retry path. Do
    # not hide failed headless deliveries just because they have no tmux pane.
    return (sessions is not None and signal.agent not in sessions
            and now - signal.info.st_mtime >= ORPHAN_AGE_SECONDS
            and not os.path.lexists(runtime_dir / 'persistent' / f'{signal.agent}.json'))


def counts(agent_dirs: list[str], runtime_dir: str, now: float) -> dict:
    signals = inventory(agent_dirs)
    sessions = session_names() if signals else set()
    orphans = sum(is_orphan(signal, sessions, Path(runtime_dir), now) for signal in signals)
    return {'signal_count': len(signals), 'orphan_signal_count': orphans,
            'actionable_signal_count': len(signals) - orphans,
            'signal_sessions_known': sessions is not None}


@contextmanager
def _directory_fd(path: Path):
    """Anchor every ancestor without following links, including signals itself."""
    path = path.absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def prune(signals_dir: Path, runtime_dir: Path, *, apply: bool = False) -> int:
    """Only old absent-recipient signals under signals/projects/*/agents.

    Re-probe sessions immediately before unlink. Directory fds and a final
    identity/mtime check prevent following a replacement symlink or deleting a
    refreshed signal from a Mail writer. No directories or state files deleted.
    """
    root = signals_dir.absolute()
    if root.is_symlink():
        raise ValueError('signals directory must not be a symlink')
    projects = root / 'projects'
    agent_dirs = [str(path / 'agents') for path in projects.iterdir()
                  if path.is_dir() and not path.is_symlink()] if projects.exists() else []
    candidates = inventory(agent_dirs)
    sessions = session_names()
    if sessions is None:
        raise ValueError('cannot confirm tmux sessions; no signals removed')
    removed = 0
    for signal in candidates:
        if not is_orphan(signal, sessions, runtime_dir, time.time()):
            continue
        relative = signal.path.absolute().relative_to(root)
        # No recursion or arbitrary paths: exactly the Mail signal layouts.
        if len(relative.parts) not in (4, 5) or relative.parts[0] != 'projects' or relative.parts[2] != 'agents':
            continue
        try:
            with _directory_fd(root / relative.parent) as fd:
                current = os.stat(relative.name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (signal.info.st_dev, signal.info.st_ino, signal.info.st_mtime_ns, signal.info.st_size):
                    continue
                if apply:
                    # Recheck live destination and headless ownership at deletion.
                    if not is_orphan(signal, session_names(), runtime_dir, time.time()):
                        continue
                    latest = os.stat(relative.name, dir_fd=fd, follow_symlinks=False)
                    if (latest.st_dev, latest.st_ino, latest.st_mtime_ns, latest.st_size) != (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size):
                        continue
                    os.unlink(relative.name, dir_fd=fd)
                print(('removed: ' if apply else 'would remove: ') + str(signal.path))
                removed += 1
        except OSError:
            continue
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description='Preview old orphan Mail signals; --prune deletes only confirmed candidates.')
    home = Path.home() / '.agentstack'
    mail_home = Path(os.environ.get('AGENTSTACK_MAIL_HOME', str(home / 'mail')))
    parser.add_argument('--signals-dir', type=Path, default=Path(os.environ.get('AGENTSTACK_SIGNALS_DIR', str(mail_home / 'signals'))))
    parser.add_argument('--runtime-dir', type=Path, default=Path(os.environ.get('AGENTSTACK_RUNTIME_DIR', str(home / 'runtime'))))
    parser.add_argument('--prune', action='store_true')
    args = parser.parse_args()
    try:
        count = prune(args.signals_dir.expanduser(), args.runtime_dir.expanduser(), apply=args.prune)
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(f'{count} signals ' + ('removed' if args.prune else 'eligible; rerun with --prune to remove'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
