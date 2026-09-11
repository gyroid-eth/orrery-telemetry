"""Add Antigravity / Gemini to the Dashboard NEW AGENT control plane.

The core dashboard stays provider-agnostic.  This optional extension is loaded
only by ``provider_server.py`` when the Gemini provider payload is installed.
It is the single owner of the Gemini Dashboard policy: the catalog entry, the
server-side validation, and the capability-driven modal controls all read the
same declaration below.  Registration, contact policy, token handoff, and
readiness stay in the core; this module only resolves a trusted
``SpawnLaunchSpec`` for ``spawn_gemini_preregistered.sh``.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from typing import Any


PROVIDER_ID = "gemini"
_DEFAULT_MODELS = (
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
)
_EFFORTS = ("low", "medium", "high")
# Dashboard launches require an explicit effort.  The direct CLI launchers keep
# their historical ``high`` fallback; this declaration does not reach them.
CAPABILITIES = {
    "effort": True,
    "effort_required": True,
    "mcp": True,
    "resume": False,
    "runtime": True,
    "transcript": False,
    "standalone": False,
    "worktree_required": True,
    "resources_required": True,
}
_ADAPTER_NAME = "spawn_gemini_preregistered.sh"
_HELPER_NAMES = (
    "agentstack-gemini-child-mail",
    "agentstack-gemini-stream",
    "agentstack-gemini-mcp",
)
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_GLOB_CHARS = frozenset("*?[")
_MAX_RESOURCES = 64
_MAX_RESOURCE_LENGTH = 1024
# Directory entries the symlink boundary walk may read for one launch.
_MAX_BOUNDARY_ENTRIES = 200_000
# Symlink expansions traced hop by hop for one path before deferring to realpath.
_MAX_SYMLINK_HOPS = 40

_log = logging.getLogger("agentstack.dashboard.gemini")


# --------------------------------------------------------------------------- #
# Catalog / model policy
# --------------------------------------------------------------------------- #
def _gemini_models(base: Any) -> tuple[list[str], str]:
    """Return the Gemini model allow-list, or an error that disables it."""
    values: list[str] = []
    for raw in os.environ.get("AGENTSTACK_GEMINI_MODELS", "").split(","):
        value = raw.strip()
        if value and value not in values:
            values.append(value)
    models = values or list(_DEFAULT_MODELS)
    native = (
        ("claude", set(base._SPAWN_MODELS)),
        ("codex", set(base._codex_models())),
    )
    for model in models:
        if _MODEL_RE.fullmatch(model) is None:
            return [], f"invalid model id for provider gemini: {model!r}"
        for provider, ids in native:
            if model in ids:
                # A shared id would make the model string ambiguous for every
                # consumer that infers a provider from it.
                return [], (
                    "model allow-list for provider gemini collides with "
                    f"provider {provider}: {model}"
                )
    return models, ""


def _catalog_item(models: list[str]) -> dict:
    return {
        "id": PROVIDER_ID,
        "label": "Antigravity",
        "program": "antigravity",
        "models": list(models),
        "default_model": models[0],
        "efforts": list(_EFFORTS),
        "effort_default": "" if CAPABILITIES["effort_required"] else "high",
        "provider_key": "google",
        "capabilities": dict(CAPABILITIES),
    }


# --------------------------------------------------------------------------- #
# Resource declarations
# --------------------------------------------------------------------------- #
def normalize_resources(raw: Any) -> str:
    """Return the canonical comma-separated resource form or raise ValueError.

    Resources are reservation patterns relative to the repository root that
    the child's isolated worktree mirrors.  Anything that could name a path
    outside that root is rejected rather than rewritten.
    """
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise ValueError("resources must be a comma-separated string")
    if _CONTROL_RE.search(raw):
        raise ValueError("resource contains a control character")
    canonical: list[str] = []
    for item in (value.strip() for value in raw.split(",")):
        if not item:
            continue
        if len(item) > _MAX_RESOURCE_LENGTH:
            raise ValueError("resource is too long")
        if item.startswith("~"):
            raise ValueError(f"resource must not use home expansion: {item}")
        if item.startswith("/"):
            raise ValueError(f"resource must be relative to the repository: {item}")
        if "\\" in item:
            raise ValueError(f"resource must use '/' separators: {item}")
        if item.startswith("-"):
            raise ValueError(f"resource must not start with '-': {item}")
        parts = [part for part in item.split("/") if part not in ("", ".")]
        # ``./-rf`` normalizes to ``-rf``; check the form that is handed on.
        if parts and parts[0].startswith("-"):
            raise ValueError(f"resource must not start with '-': {item}")
        if ".." in parts:
            raise ValueError(f"resource must not contain parent traversal: {item}")
        if any(part.casefold() == ".git" for part in parts):
            raise ValueError(f"resource must not name git metadata: {item}")
        if not parts:
            raise ValueError(f"resource must name a path inside the repository: {item}")
        value = "/".join(parts)
        if value not in canonical:
            canonical.append(value)
    if not canonical:
        raise ValueError("resources required for provider gemini")
    if len(canonical) > _MAX_RESOURCES:
        raise ValueError(f"at most {_MAX_RESOURCES} resources may be declared")
    return ",".join(canonical)


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _names_git(path: str, root: str) -> bool:
    """Return whether ``path`` inside ``root`` has a ``.git`` component."""
    return _is_within(path, root) and any(
        part.casefold() == ".git" for part in os.path.relpath(path, root).split(os.sep)
    )


def _reaches_git(root_real: str, base: str, names: list[str]) -> bool:
    """Return whether resolving ``names`` below real directory ``base`` reaches Git metadata.

    Symlinks are expanded one hop at a time and every location visited inside
    ``root_real`` is classified by its root-relative components, so an alias
    such as ``src/meta -> ../.git`` is caught whether ``.git`` is a directory,
    a linked worktree's gitdir file, or a symlink to a differently named store.
    The fully resolved path is classified the same way.
    """
    current = base
    pending = list(reversed(names))
    hops = 0
    while pending and hops <= _MAX_SYMLINK_HOPS:
        name = pending.pop()
        if name in ("", "."):
            continue
        if name == "..":
            current = os.path.dirname(current)
            continue
        candidate = os.path.join(current, name)
        if _names_git(candidate, root_real):
            return True
        try:
            target = os.readlink(candidate)
        except OSError:
            current = candidate
            continue
        hops += 1
        if target.startswith("/"):
            current = "/"
        pending.extend(reversed(target.split("/")))
    return _names_git(os.path.realpath(os.path.join(base, *names)), root_real)


def _glob_states(parts: list[str], positions: set[int]) -> frozenset[int]:
    """Close pattern positions over ``**``, which also matches zero components."""
    closed: set[int] = set()
    pending = list(positions)
    while pending:
        index = pending.pop()
        if index in closed:
            continue
        closed.add(index)
        if index < len(parts) and parts[index] == "**":
            pending.append(index + 1)
    if len(parts) in closed:
        return frozenset((len(parts),))
    return frozenset(closed)


def _glob_step(parts: list[str], states: frozenset[int], name: str) -> frozenset[int]:
    """Advance pattern positions past one path component.

    A position equal to ``len(parts)`` means the path already matched; a
    matched directory covers everything beneath it, as a reservation does.
    """
    done = len(parts)
    if done in states:
        return frozenset((done,))
    following: set[int] = set()
    for index in states:
        if parts[index] == "**":
            following.add(index)
        elif _component_matches(name, parts[index]):
            following.add(index + 1)
    return _glob_states(parts, following)


def _fold(text: str) -> str:
    """Canonical caseless form: what default APFS compares, full folding included."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", text).casefold())


def _trim_marks(text: str, start: bool, end: bool) -> str:
    while start and text and unicodedata.combining(text[0]):
        text = text[1:]
    while end and text and unicodedata.combining(text[-1]):
        text = text[:-1]
    return text


_FOLDED_PATTERNS: dict[str, re.Pattern[str]] = {}


def _folded_pattern(part: str) -> re.Pattern[str]:
    """Compile ``part`` to match ``_fold`` of any name a folding filesystem equates.

    A literal folds to the text it names.  A positive class of ASCII members
    folds to their lowercase forms.  ``?`` and any other class can name a
    character that folds to several (``ß`` to ``ss``) or that ``[!s]`` excludes
    only in one case, so they widen to one or more characters.  Combining marks
    at a literal's edge next to such a wildcard can be reordered across it by
    normalization, so the wildcard absorbs them.
    """
    compiled = _FOLDED_PATTERNS.get(part)
    if compiled is not None:
        return compiled
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(part):
        char = part[index]
        index += 1
        if char == "[":
            end = index + (part[index:index + 1] == "!")
            end = part.find("]", end + (part[end:end + 1] == "]"))
            if end >= 0:
                token, index = part[index - 1:end + 1], end + 1
                # Ask fnmatch whether an ASCII class is negated: dropping an
                # empty range turns ``[a-[!.]`` into ``[!.]``.
                if token.isascii() and not fnmatch.fnmatchcase("\u0100", token):
                    members = sorted({
                        chr(code).lower() for code in range(128)
                        if fnmatch.fnmatchcase(chr(code), token)
                    })
                    tokens.append(("class", f"[{''.join(map(re.escape, members))}]"
                                   if members else "(?!)"))
                else:
                    tokens.append(("wild", ".+"))
                continue
        if char in "*?":
            tokens.append(("wild", ".*" if char == "*" else ".+"))
        elif tokens and tokens[-1][0] == "literal":
            tokens[-1] = ("literal", tokens[-1][1] + char)
        else:
            tokens.append(("literal", char))
    regex = []
    for position, (kind, value) in enumerate(tokens):
        if kind == "literal":
            start = position > 0 and tokens[position - 1][0] == "wild"
            end = position + 1 < len(tokens) and tokens[position + 1][0] == "wild"
            value = _trim_marks(unicodedata.normalize("NFD", value), start, end)
            value = re.escape(_trim_marks(_fold(value), start, end))
        regex.append(value)
    compiled = _FOLDED_PATTERNS[part] = re.compile("".join(regex), re.S)
    return compiled


def _component_matches(name: str, part: str) -> bool:
    """Return whether pattern component ``part`` may open the entry ``name``.

    Literal checks casefold, and on a case-insensitive filesystem such as
    default APFS ``SR?`` opens ``src`` and ``.GI?`` opens ``.git``.  A component
    matches exactly, after casefolding both sides, or through
    ``_folded_pattern``.  Folding only ever adds matches, and depends on names
    alone, so a case-sensitive filesystem gets the same, stricter, answer.
    """
    try:
        return (
            fnmatch.fnmatchcase(name, part)
            or fnmatch.fnmatchcase(name.casefold(), part.casefold())
            or _folded_pattern(part).fullmatch(_fold(name)) is not None
        )
    except re.error:
        # Some interpreters reject a reversed range such as casefolded ``[Z-a]``.
        return True


class _BoundaryUnverifiable(Exception):
    """The walk could not finish, so the resource cannot be accepted."""


class _GitMetadataCovered(Exception):
    """A resource pattern covered an existing Git metadata entry."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def _glob_escape(root_real: str, literal: list[str], parts: list[str], budget: list[int]) -> str:
    """Return the first covered repo-relative path whose symlink leaves ``root_real``.

    Walks from the literal prefix without following symlinks implicitly.  Every
    symlink on a path the remaining ``parts`` can match, or could still match
    below, is resolved; one that stays inside the root is descended into as
    the directory it names.  Each (real directory, pattern state) pair is
    scanned once, so symlink cycles terminate, and ``budget`` caps the total
    number of entries read across all resources.  Existing ``.git`` entries
    are reported before they can be reserved, whether directories, files, or
    symlinks, and so is any inside symlink on a covered path that resolves
    into Git metadata, since everything beneath such an alias is metadata.
    """
    start_real = os.path.realpath(os.path.join(root_real, *literal))
    start_git = _reaches_git(root_real, root_real, literal)
    pending = [(start_real, "/".join(literal), _glob_states(parts, {0}), start_git)]
    seen: set[tuple[str, frozenset[int], bool]] = set()
    first_escape = ""
    while pending:
        directory, relative, states, in_git = pending.pop()
        if (directory, states, in_git) in seen:
            continue
        seen.add((directory, states, in_git))
        try:
            entries = os.scandir(directory)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise _BoundaryUnverifiable(relative or ".") from exc
        try:
            with entries:
                for entry in entries:
                    budget[0] -= 1
                    if budget[0] < 0:
                        raise _BoundaryUnverifiable("too many entries")
                    following = _glob_step(parts, states, entry.name)
                    if not following:
                        continue
                    path = f"{relative}/{entry.name}" if relative else entry.name
                    covered_git = in_git or entry.name.casefold() == ".git"
                    if covered_git and len(parts) in following:
                        raise _GitMetadataCovered(path)
                    if entry.is_symlink():
                        target = os.path.realpath(entry.path)
                        if not _is_within(target, root_real):
                            if not first_escape:
                                first_escape = path
                            continue
                        if _reaches_git(root_real, directory, [entry.name]):
                            raise _GitMetadataCovered(path)
                        if os.path.isdir(target):
                            pending.append((target, path, following, covered_git))
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append((entry.path, path, following, covered_git))
        except OSError as exc:
            raise _BoundaryUnverifiable(relative or ".") from exc
    return first_escape


def resource_boundary_error(root: str, resources: str) -> str:
    """Return an error when a canonical resource escapes ``root`` via symlinks.

    Every existing literal component is resolved, then every existing path the
    resource covers is walked: wildcard and ``**`` components at any depth, and
    everything beneath a matched or literal directory.  A literal prefix or
    covered symlink that resolves into Git metadata is rejected after any
    literal-prefix escape.  Missing paths are accepted; a walk that cannot finish (permission error, or more than
    ``_MAX_BOUNDARY_ENTRIES`` entries) fails closed.
    """
    root_real = os.path.realpath(root)
    budget = [_MAX_BOUNDARY_ENTRIES]
    for resource in resources.split(","):
        parts = resource.split("/")
        literal: list[str] = []
        for part in parts:
            if _GLOB_CHARS.intersection(part):
                break
            literal.append(part)
        for index in range(1, len(literal) + 1):
            resolved = os.path.realpath(os.path.join(root_real, *literal[:index]))
            if not _is_within(resolved, root_real):
                return f"resource escapes the repository root through a symlink: {resource}"
        for index in range(1, len(literal) + 1):
            if _reaches_git(root_real, root_real, literal[:index]):
                return (
                    "resource must not cover git metadata: "
                    f"{resource} ({'/'.join(literal[:index])})"
                )
        try:
            escape = _glob_escape(root_real, literal, parts[len(literal):], budget)
        except _GitMetadataCovered as exc:
            return f"resource must not cover git metadata: {resource} ({exc.path})"
        except _BoundaryUnverifiable as exc:
            if budget[0] < 0:
                return (
                    "resource covers too many paths to verify the repository "
                    f"boundary; declare narrower resources: {resource}"
                )
            return f"could not verify the repository boundary for resource: {resource} ({exc})"
        if escape:
            return (
                "resource escapes the repository root through a symlink: "
                f"{resource} ({escape})"
            )
    return ""


# --------------------------------------------------------------------------- #
# Pre-registration preflight
# --------------------------------------------------------------------------- #
def _launch_path() -> str:
    """PATH the core hands to the launcher (see do_spawn's ~/.local/bin rule)."""
    home_local = os.path.expanduser("~/.local/bin")
    current = os.environ.get("PATH", "")
    if home_local in current.split(":"):
        return current
    return f"{home_local}:{current}" if current else home_local


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _which(command: str, path: str) -> str:
    if "/" in command:
        return command if _executable(command) else ""
    return shutil.which(command, path=path) or ""


def _git(git: str, *args: str) -> str:
    try:
        result = subprocess.run(
            [git, *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _preflight(base: Any, request: dict, resources: str) -> tuple[dict, str]:
    """Check what the adapter needs before an identity is registered.

    The adapter repeats these checks; failing here first avoids leaving a
    retained registration behind for a launch that could never start.
    """
    hooks_dir = os.path.abspath(os.path.expanduser(base.HOOKS_DIR))
    adapter = os.path.join(hooks_dir, _ADAPTER_NAME)
    if not _executable(adapter):
        return {}, f"spawn adapter missing for provider gemini: {adapter}"
    home = os.path.abspath(os.path.expanduser(
        os.environ.get("AGENTSTACK_HOME") or os.path.dirname(hooks_dir)
    ))
    for name in _HELPER_NAMES:
        helper = os.path.join(home, "bin", name)
        if not _executable(helper):
            return {}, f"Gemini provider helper is not installed: {helper}"
    path = _launch_path()
    if not _which(os.environ.get("AGENTSTACK_GEMINI_BIN") or "agy", path):
        return {}, "Antigravity CLI not found for provider gemini (expected agy)"
    for command, label in (
        (os.environ.get("AGENTSTACK_PYTHON") or "python3", "selected Python"),
        ("tmux", "tmux"),
    ):
        if not _which(command, path):
            return {}, f"{label} not found for provider gemini"
    git = _which("git", path)
    if not git:
        return {}, "git not found for provider gemini"

    base_rev = request["worktree_base"] or "HEAD"
    if base_rev.startswith("-"):
        return {}, f"worktree base must not start with '-': {base_rev}"
    work_dir = request["work_dir"]
    if not os.path.isdir(work_dir):
        return {}, f"dir does not exist: {work_dir}"
    source_repo = _git(git, "-C", work_dir, "rev-parse", "--show-toplevel")
    if not source_repo:
        return {}, "provider gemini requires a git repository"
    if not _git(git, "-C", source_repo, "rev-parse", "--verify", "--quiet",
                f"{base_rev}^{{commit}}"):
        return {}, f"could not resolve worktree base for provider gemini: {base_rev}"
    boundary = resource_boundary_error(source_repo, resources)
    if boundary:
        return {}, boundary
    if not base._project_key():
        return {}, "AGENTSTACK_PROJECT_KEY or AGENTSTACK_VAULT is not configured"
    if not base._runtime_agent_token(request["parent"]):
        return {}, f"parent registration token unavailable for '{request['parent']}'"
    return {"adapter": adapter, "hooks_dir": hooks_dir, "home": home}, ""


def _write_task_file(base: Any, task: str) -> str:
    os.makedirs(base.RUNTIME_DIR, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(
        prefix="provider-gemini-task-",
        suffix=".txt",
        dir=base.RUNTIME_DIR,
        text=True,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(task)
    except BaseException:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise
    return path


# --------------------------------------------------------------------------- #
# Capability-driven modal controls
# --------------------------------------------------------------------------- #
_UI_HELPERS = """/* provider-capabilities:start */
let spmWorktreeForced=false,spmWorktreeChoice=false;
function spawnSelectedProviderCapabilities(){
  const provider=spmProviders.find(item=>item.id===spmSelectedProvider);
  return provider&&provider.capabilities||{};
}
function spawnProviderAcceptsResources(caps){
  return !!(caps&&(caps.resources_required||caps.resources));
}
function spawnProviderRequirementsMet(){
  const caps=spawnSelectedProviderCapabilities();
  const input=SPM('spm-resources');
  if(caps.resources_required&&
     !(input&&input.value.split(',').some(item=>item.trim())))return false;
  if(caps.effort_required&&!spmSelectedEffort)return false;
  return true;
}
function applySpawnProviderCapabilities(provider){
  const caps=provider&&provider.capabilities||{};
  const acceptsResources=spawnProviderAcceptsResources(caps);
  const row=SPM('spm-resources-row');
  const input=SPM('spm-resources');
  if(row)row.style.display=acceptsResources?'grid':'none';
  if(input){
    // Declarations belong to the provider that asked for them.
    if(!acceptsResources)input.value='';
    input.oninput=updateSpawnButton;
  }
  const worktree=SPM('spm-worktree');
  if(worktree){
    if(caps.worktree_required){
      if(!spmWorktreeForced)spmWorktreeChoice=worktree.checked;
      spmWorktreeForced=true;
      worktree.checked=true;
    }else if(spmWorktreeForced){
      // Give back the isolation choice that was in place before forcing.
      spmWorktreeForced=false;
      worktree.checked=spmWorktreeChoice;
    }
    worktree.disabled=!!caps.worktree_required;
    SPM('spm-wt-base').classList.toggle('on',worktree.checked);
  }
}
function resetSpawnProviderCapabilities(){
  spmWorktreeForced=false;spmWorktreeChoice=false;
  if(SPM('spm-worktree'))SPM('spm-worktree').disabled=false;
  if(SPM('spm-resources'))SPM('spm-resources').value='';
  if(SPM('spm-resources-row'))SPM('spm-resources-row').style.display='none';
}
/* provider-capabilities:end */
"""

_UI_PATCHES: tuple[tuple[str, str, str], ...] = (
    (
        "resource row",
        """          <div class="spm-row full">
            <label class="spm-lab">isolation</label>""",
        """          <div class="spm-row full" id="spm-resources-row" style="display:none">
            <label class="spm-lab" for="spm-resources">resources</label>
            <input type="text" id="spm-resources" placeholder="src/**,tests/**" autocomplete="off">
            <div class="spm-hint">Comma-separated repository paths reserved for providers that require resource isolation.</div>
          </div>
          <div class="spm-row full">
            <label class="spm-lab">isolation</label>""",
    ),
    (
        "provider capability normalization",
        """    return {id,label:String(provider&&provider.label||id).trim(),
      models,defaultModel,efforts,
      defaultEffort:String(provider&&provider.effort_default||'').trim()};""",
        """    const capabilities=(provider&&provider.capabilities&&typeof provider.capabilities==='object')
      ?provider.capabilities:{};
    return {id,label:String(provider&&provider.label||id).trim(),
      models,defaultModel,efforts,
      defaultEffort:String(provider&&provider.effort_default||'').trim(),
      capabilities};""",
    ),
    (
        "provider selection",
        """function selectSpawnProvider(providerId){
  const provider=spmProviders.find(item=>item.id===providerId);
  spmSelectedProvider=provider?provider.id:'';
  spmSelectedModel='';
  spmSelectedEffort='';
""",
        _UI_HELPERS + """function selectSpawnProvider(providerId){
  const provider=spmProviders.find(item=>item.id===providerId);
  spmSelectedProvider=provider?provider.id:'';
  spmSelectedModel='';
  spmSelectedEffort='';
  applySpawnProviderCapabilities(provider);
""",
    ),
    (
        "explicit effort selection",
        """  const fallback=efforts.includes(provider&&provider.defaultEffort)
    ? provider.defaultEffort:(efforts[0]||'');
  selectSpawnEffort(fallback);""",
        """  const effortRequired=!!(provider&&provider.capabilities&&provider.capabilities.effort_required);
  const fallback=effortRequired?'':(
    efforts.includes(provider&&provider.defaultEffort)
      ?provider.defaultEffort:(efforts[0]||''));
  selectSpawnEffort(fallback);""",
    ),
    (
        "effort button refresh",
        """  hint.textContent=copy?`${spmSelectedEffort} · ${copy}`:'';
  renderSpawnEngineNote();
}""",
        """  hint.textContent=copy?`${spmSelectedEffort} · ${copy}`:'';
  renderSpawnEngineNote();
  updateSpawnButton();
}""",
    ),
    (
        "launch readiness gate",
        """  const identityReady=!spmSelectedName||spmIdentityState==='verified';
  button.disabled=spmBusy||!spmReady||!identityReady;""",
        """  const identityReady=!spmSelectedName||spmIdentityState==='verified';
  const providerReady=spawnProviderRequirementsMet();
  button.disabled=spmBusy||!spmReady||!identityReady||!providerReady;""",
    ),
    (
        "resource payload",
        """  if(spmSelectedEffort)payload.effort=spmSelectedEffort;
  if(SPM('spm-worktree').checked){""",
        """  if(spmSelectedEffort)payload.effort=spmSelectedEffort;
  const resources=(SPM('spm-resources')&&SPM('spm-resources').value||'').trim();
  if(spawnProviderAcceptsResources(spawnSelectedProviderCapabilities())&&resources)
    payload.resources=resources;
  if(SPM('spm-worktree').checked){""",
    ),
    (
        "modal reset",
        """  SPM('spm-worktree').checked=false;
  SPM('spm-wt-base').classList.remove('on');""",
        """  SPM('spm-worktree').checked=false;
  resetSpawnProviderCapabilities();
  SPM('spm-wt-base').classList.remove('on');""",
    ),
)


def apply_ui_patches(text: str) -> tuple[str, str]:
    """Apply every modal patch or none of them.

    Returns ``(patched, "")`` on success and ``(text, error)`` when any target
    is missing or ambiguous, so a drifted page is served unmodified instead of
    with a partial provider integration.
    """
    patched = text
    for label, old, new in _UI_PATCHES:
        count = patched.count(old)
        if count != 1:
            state = "missing" if count == 0 else "ambiguous"
            return text, f"dashboard UI patch target {state}: {label}"
        patched = patched.replace(old, new, 1)
    return patched, ""


# --------------------------------------------------------------------------- #
# Installation into the provider-aware dashboard
# --------------------------------------------------------------------------- #
class _Integration:
    def __init__(self, base: Any) -> None:
        self.base = base
        self._lock = threading.Lock()
        self._ui_key: tuple | None = None
        self._ui_error = ""
        self._reported: set[str] = set()

    def report(self, error: str) -> None:
        with self._lock:
            if error in self._reported:
                return
            self._reported.add(error)
        _log.error("provider gemini disabled: %s", error)

    def ui_error(self) -> str:
        path = self.base.INDEX_HTML
        try:
            stat = os.stat(path)
        except OSError as exc:
            return f"dashboard index unavailable: {exc}"
        key = (path, stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._ui_key == key:
                return self._ui_error
        try:
            with open(path, encoding="utf-8") as handle:
                _patched, error = apply_ui_patches(handle.read())
        except (OSError, UnicodeDecodeError) as exc:
            error = f"dashboard index unavailable: {exc}"
        with self._lock:
            self._ui_key, self._ui_error = key, error
        return error

    def availability(self) -> tuple[list[str], str]:
        """Return the model allow-list, or why Gemini fails closed."""
        models, error = _gemini_models(self.base)
        if not error:
            error = self.ui_error()
        if error:
            self.report(error)
            return [], error
        return models, ""


def _install_catalog(base: Any, integration: _Integration) -> None:
    original = base.spawn_names_payload

    def spawn_names_payload() -> dict:
        payload = original()
        providers = payload.get("providers")
        # The core's unavailable payload (win32) carries no provider list.
        if not isinstance(providers, list):
            return payload
        if any(isinstance(item, dict) and item.get("id") == PROVIDER_ID
               for item in providers):
            return payload
        models, error = integration.availability()
        if error:
            return payload
        payload["providers"] = [*providers, _catalog_item(models)]
        return payload

    base.spawn_names_payload = spawn_names_payload


def _install_spawn(base: Any, integration: _Integration) -> None:
    original = base.do_spawn

    def do_spawn(payload: dict) -> dict:
        raw_provider = payload.get("provider") if isinstance(payload, dict) else None
        if not isinstance(raw_provider, str) or raw_provider.strip().lower() != PROVIDER_ID:
            # Claude, Codex, and malformed payloads keep the canonical path.
            return original(payload)

        unavailable = base._spawn_unavailable_error()
        if unavailable:
            return unavailable
        request, error = base._spawn_request(payload)
        if error:
            return error
        if request["standalone"] and not CAPABILITIES["standalone"]:
            return {"ok": False, "error": "standalone not supported for provider gemini"}
        models, error = integration.availability()
        if error:
            return {"ok": False, "error": f"provider gemini unavailable: {error}"}
        model = str(payload.get("model") or models[0]).strip()
        if model not in models:
            return {"ok": False, "error": f"model not allowed for provider gemini: {model}"}
        effort = str(payload.get("effort") or "").strip().lower()
        if not effort:
            if CAPABILITIES["effort_required"]:
                return {"ok": False, "error": "effort required for provider gemini"}
            effort = "high"
        if effort not in _EFFORTS:
            return {"ok": False, "error": f"effort not allowed for provider gemini: {effort}"}
        try:
            resources = normalize_resources(payload.get("resources"))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        context, error = _preflight(base, request, resources)
        if error:
            return {"ok": False, "error": error}

        task_file = _write_task_file(base, request["task"])
        try:
            spec = base.SpawnLaunchSpec(
                provider=PROVIDER_ID,
                program="antigravity",
                model=model,
                script=context["adapter"],
                effort=effort,
                standalone_supported=CAPABILITIES["standalone"],
                worktree_required=CAPABILITIES["worktree_required"],
                # The adapter hands the full task to Antigravity and reports the
                # result itself; a task mail would be a second, conflicting copy.
                task_mail=False,
                launcher_env=(
                    ("AGENTSTACK_HOOKS_DIR", context["hooks_dir"]),
                    ("AGENTSTACK_HOME", context["home"]),
                    ("AGENTSTACK_GEMINI_MODEL", model),
                    ("AGENTSTACK_GEMINI_EFFORT", effort),
                    ("AGENTSTACK_GEMINI_RESOURCES", resources),
                    ("AGENTSTACK_GEMINI_TASK_FILE", task_file),
                ),
                handoff_paths=(task_file,),
                signal_process_group=True,
            )
        except BaseException:
            os.unlink(task_file)
            raise
        # The core owns task_file from here and removes it after the adapter.
        return base.spawn_with_launch_spec(payload, spec)

    base.do_spawn = do_spawn


def _install_render(base: Any, integration: _Integration) -> None:
    original = base._render_dashboard_index

    def _render_dashboard_index(source: bytes, *args: Any, **kwargs: Any) -> bytes:
        rendered = original(source, *args, **kwargs)
        try:
            text = rendered.decode("utf-8")
        except UnicodeDecodeError:
            return rendered
        patched, error = apply_ui_patches(text)
        if error:
            integration.report(error)
            return rendered
        return patched.encode("utf-8")

    base._render_dashboard_index = _render_dashboard_index


def install(base: Any) -> Any:
    if getattr(base, "_GEMINI_PROVIDER_RUNTIME_INSTALLED", False):
        return base
    for name in ("SpawnLaunchSpec", "spawn_with_launch_spec",
                 "_spawn_request", "_spawn_unavailable_error"):
        if not hasattr(base, name):
            raise RuntimeError(
                f"dashboard core lacks the provider launch contract: {name}"
            )
    integration = _Integration(base)
    _install_catalog(base, integration)
    _install_spawn(base, integration)
    _install_render(base, integration)
    base._GEMINI_PROVIDER_RUNTIME_INSTALLED = True
    return base
