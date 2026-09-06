"""Read launcher-owned vocabulary without executing shell code on Windows."""
import json
import os
from pathlib import Path
import re

UNAVAILABLE = "Native Windows spawn is not supported. Use WSL2, the primary Windows path."


def load_vocabulary(script: str) -> tuple[list[str], list[str]]:
    """Return scientists, adjectives; reject shell syntax we cannot interpret.

    The checked-in array is plain ASCII words. This is deliberately not a Bash
    interpreter: expansion, quoting, substitutions and duplicate declarations
    require review rather than silently producing a different vocabulary.
    """
    source = Path(script)
    try:
        text = source.read_text(encoding="utf-8")
        arrays = re.findall(
            r"^AGS_SIMPLE_ADJECTIVES=\(\s*\n([^)]*)^\)\s*$", text, re.MULTILINE
        )
        declarations = re.findall(r"^\s*AGS_SIMPLE_ADJECTIVES\s*\+?=", text, re.MULTILINE)
        if len(declarations) != 1 or len(arrays) != 1 or not re.fullmatch(r"[A-Za-z \t\r\n]+", arrays[0]):
            raise ValueError("unsupported adjective array syntax")
        adjectives = arrays[0].split()
        if not adjectives or len(set(adjectives)) != len(adjectives):
            raise ValueError("empty or duplicate adjectives")
        json_path = os.environ.get("AGENTSTACK_SCIENTISTS_JSON") or str(
            source.parent.parent.parent / "dashboard" / "scientist_portraits.json"
        )
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("scientist JSON must be an object")
        scientists = [name for name in sorted(data) if name.isascii() and name.isalpha()]
        if not scientists:
            raise ValueError("empty scientist vocabulary")
        return scientists, adjectives
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"scientist vocabulary unavailable: {exc}") from exc
