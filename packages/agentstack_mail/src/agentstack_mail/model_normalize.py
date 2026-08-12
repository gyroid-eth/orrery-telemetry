"""Read-time normalization of free-form agent ``model`` strings.

Each session supplies its own ``model`` string at ``register_agent`` time, so
the same underlying model shows up under many spellings (``claude-opus-4-7``,
``opus-4.7``, ``claude-opus-4-7[1m]``, ``opus 4.7`` ...). This module folds
those spellings to one canonical id plus a human display label.

Design
------
- **Structural, not enumerated.** It parses ``<family>-<version>[-variant]``
  rather than keeping an alias table, so *future* versions
  (``opus-4.8``, ``sonnet-4.7`` ...) and the ``[1m]`` / ``-1m`` context
  markers are handled with **zero code changes**.
- **Non-destructive.** It is applied only when an agent record is serialized;
  the raw value stays in the database. Improving a rule therefore fixes every
  historical row retroactively, with no migration.
- **Honest about codenames.** A pure codename with no family/version structure
  (e.g. ``mythos``) cannot be resolved algorithmically. Such values pass
  through unchanged and are logged once, so a human can add a single line to
  :data:`ALIASES` after learning what the codename maps to.
"""
# Derived from the frozen MCP Agent Mail live baseline.
# See NOTICE.md, UPSTREAM_LICENSE, and AGENTSTACK_LICENSE for provenance and terms.

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Irregular codenames -> canonical id. Normally empty. When an unrecognized
# model appears in ``unrecognized_models.log`` and you learn its family,
# add one line here, e.g. ``"mythos": "opus-4.8"``. Because normalization is
# read-time, that single line fixes past and future rows alike.
ALIASES: dict[str, str] = {}

_KNOWN_FAMILIES = (
    "fable",
    "mythos",
    "opus",
    "sonnet",
    "haiku",
    "gpt",
    "gemini",
    "grok",
    "llama",
    "qwen",
    "mistral",
    "deepseek",
)

# <family>  optional sep  <major[.minor]>  optional recognized variant
_FAMILY_RE = re.compile(
    r"(?P<family>" + "|".join(_KNOWN_FAMILIES) + r")"
    r"[-_. ]*"
    r"(?P<version>\d+(?:[.\-]\d+)?)"
    r"(?P<variant>[-_]?(?:codex|thinking|mini|nano|turbo|flash|pro|preview|exp))?"
)
_VENDOR_PREFIX_RE = re.compile(r"^(?:claude|anthropic|openai|google)[-_. ]*")
# Trailing context-window marker: ``[1m]`` / ``-1m`` / ``1m-context`` / ``(2m)``
_CONTEXT_SUFFIX_RE = re.compile(r"[\[\(\-_ ]*\d+\s*m(?:[-_ ]?context)?[\]\)]*\s*$")

_DISPLAY_FAMILY = {
    "opus": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
    "gpt": "GPT",
    "gemini": "Gemini",
    "grok": "Grok",
    "llama": "Llama",
    "qwen": "Qwen",
    "mistral": "Mistral",
    "deepseek": "DeepSeek",
}

_seen_unrecognized: set[str] = set()
_seen_lock = threading.Lock()


def _log_unrecognized(raw: str) -> None:
    """Log an unrecognized model string once. Never raises."""
    key = raw.strip().lower()
    with _seen_lock:
        if key in _seen_unrecognized:
            return
        _seen_unrecognized.add(key)
    logger.warning(
        "Unrecognized agent model %r left un-normalized. If this is a known "
        "model, add an alias in agentstack_mail.model_normalize.ALIASES "
        "(e.g. {%r: 'opus-4.8'}).",
        raw,
        key,
    )
    try:  # best-effort persistent breadcrumb; logging must never break serialization
        import datetime
        import os

        from .config import get_settings

        root = os.path.expanduser(get_settings().storage_settings.root)
        os.makedirs(root, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(os.path.join(root, "unrecognized_models.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}\t{raw}\n")
    except Exception:
        pass


def normalize_model(raw: Optional[str]) -> str:
    """Return a stable canonical model id for a free-form model string.

    Examples
    --------
    >>> normalize_model("claude-opus-4-7[1m]")
    'opus-4.7'
    >>> normalize_model("opus 4.7")
    'opus-4.7'
    >>> normalize_model("claude-opus-4-8")      # future version, no code change
    'opus-4.8'
    >>> normalize_model("claude-haiku-4-5-20251001")
    'haiku-4.5'
    >>> normalize_model("gpt-5-codex")
    'gpt-5-codex'
    >>> normalize_model("mythos")               # unknown codename -> passthrough
    'mythos'
    """
    if not raw or not raw.strip():
        return ""
    original = raw.strip()
    s = original.lower()
    s = ALIASES.get(s, s)
    s = _VENDOR_PREFIX_RE.sub("", s)
    s = _CONTEXT_SUFFIX_RE.sub("", s).strip()
    m = _FAMILY_RE.search(s)
    if not m:
        _log_unrecognized(original)
        return original
    family = m.group("family")
    version = m.group("version").replace("-", ".")
    variant = (m.group("variant") or "").lstrip("-_")
    canonical = f"{family}-{version}"
    if variant:
        canonical += f"-{variant}"
    return canonical


def display_model(raw: Optional[str]) -> str:
    """Human-friendly label for a model string, e.g. ``Opus 4.7``.

    Examples
    --------
    >>> display_model("claude-opus-4-7[1m]")
    'Opus 4.7'
    >>> display_model("gpt-5-codex")
    'GPT 5 codex'
    >>> display_model("mythos")
    'mythos'
    """
    canonical = normalize_model(raw)
    if not canonical:
        return ""
    m = _FAMILY_RE.search(canonical)
    if not m:
        return canonical
    family = _DISPLAY_FAMILY.get(m.group("family"), m.group("family").title())
    label = f"{family} {m.group('version')}"
    variant = (m.group("variant") or "").lstrip("-_")
    if variant:
        label += f" {variant}"
    return label
