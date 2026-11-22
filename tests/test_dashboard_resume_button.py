"""The panel's resume button says "without Mail" before the click (#175).

A conversation-only resume has no ORRERY Mail. The summary says why, but the
button is what is clicked, so it carries the warning too, including for a
resume that still needs transcript verification.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "dashboard" / "index.html"
MAIL = "This agent cannot send or receive ORRERY Mail. ... --update-mail ..."


def _view(resume_state, capability, mode, mail, embed):
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    html = INDEX.read_text(encoding="utf-8")
    parts = []
    for pattern in (r"const RESUME_CAPABILITY_INFO=\{.*?\n\};\n",
                    r"function resumeCapabilityInfo\(code\)\{.*?\n\}\n",
                    r"function resumeButtonView\(.*?\n\}\n"):
        match = re.search(pattern, html, re.DOTALL)
        assert match, f"missing block: {pattern}"
        parts.append(match.group(0))
    args = json.dumps([resume_state, capability, mode, mail, embed])
    script = "\n".join(parts) + f"\nconsole.log(JSON.stringify(resumeButtonView(...{args})));"
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize("embed", [False, True])
@pytest.mark.parametrize("capability", ["ready", "verification_required"])
def test_conversation_only_resume_says_without_mail_with_reason_and_fix(capability, embed):
    view = _view(True, capability, "conversation_only", MAIL, embed)
    assert view["text"].endswith(" WITHOUT MAIL")
    assert MAIL in view["title"]
    if capability == "verification_required":
        assert view["text"] == "VERIFY & RESUME WITHOUT MAIL"
        # The verification explanation stays next to the Mail reason.
        assert view["title"] != MAIL and view["title"].endswith(MAIL)
    else:
        assert view["text"] == ("RESUME IN COCKPIT" if embed else "RESUME") + " WITHOUT MAIL"


@pytest.mark.parametrize("embed", [False, True])
@pytest.mark.parametrize("capability", ["ready", "verification_required"])
@pytest.mark.parametrize("mode", ["mail", "", None])
def test_mail_resume_and_unknown_mode_keep_the_plain_button(capability, embed, mode):
    view = _view(True, capability, mode, "", embed)
    assert "WITHOUT MAIL" not in view["text"] and "Mail" not in view["title"]
    assert view["text"] == ("VERIFY & RESUME" if capability == "verification_required"
                            else "RESUME IN COCKPIT" if embed else "RESUME")


def test_unavailable_and_live_rows_are_unchanged():
    assert _view(True, "credential_missing", "conversation_only", MAIL, False)["text"] == "RESUME UNAVAILABLE"
    assert _view(False, "not_required", "", "", False) == {
        "text": "OPEN TMUX", "title": "Open this live terminal session"}
    assert _view(False, "not_required", "", "", True)["text"] == "OPEN IN COCKPIT"
