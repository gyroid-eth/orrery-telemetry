"""Observe a current human-blocking control and claim one parent Mail per episode."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re

try:
    from .provider_stalls import locked_events, save_events
    from .question_screen import parse_hermes_question
    from .terminal_state import AGY_MCP_APPROVAL_CONTROLS, current_modal_activity, normalize
except ImportError:  # executable dashboard/server.py
    from provider_stalls import locked_events, save_events
    from question_screen import parse_hermes_question
    from terminal_state import AGY_MCP_APPROVAL_CONTROLS, current_modal_activity, normalize


WAIT_SECONDS = 20
PROGRAMS = {
    "claude": "claude", "claude-code": "claude", "codex": "codex", "codex-cli": "codex",
    "dsh": "dsh", "hermes": "hermes", "antigravity": "agy",
}


@dataclass(frozen=True)
class Screen:
    kind: str
    # Only fixed product UI literals belong here. These are the entire Mail quote.
    lines: tuple[str, ...]

    def message(self, name):
        quote = "\n".join("> " + line for line in self.lines)
        return (f"{name} が人の操作待ち（{self.kind}）",
                f"ORRERY Dashboard の自動通知です。子 `{name}` が `{self.kind}` の画面で"
                f"{WAIT_SECONDS}秒以上待っています。\n\n{quote}")


_CODEX_QUIT = "enter continue · esc quit"
_CODEX_BACK = "enter continue · esc back"
_CODEX_CONFIRM = "Press enter to confirm or esc to cancel"
_CLAUDE_APPROVAL_FOOTERS = {"Esc to cancel", "Esc to cancel · Tab to amend"}


def _numbered_menu(rows, options, *, cursor):
    return (sum(row.startswith(cursor) for row in rows) == 1
            and tuple(row.removeprefix(cursor) for row in rows) == options)


def _codex_choice(rows):
    footer = rows[-1]
    close = {_CODEX_QUIT: "2. Quit", _CODEX_BACK: "2. Back to Agent Command Center"}.get(footer)
    if close and "Folder access" in rows[:-3]:
        if (_numbered_menu(rows[-3:-1], ("1. Trust and continue", close), cursor="› ")
                and any(row.startswith("Trust this folder?") for row in rows[:-3])):
            return Screen("trust", ("Trust this folder?", footer))
        if (footer == _CODEX_BACK
                and _numbered_menu(rows[-3:-1], ("1. Open restricted", close), cursor="› ")
                and any(row.startswith("Config, hooks, and exec policies from untrusted folders stay disabled.")
                        for row in rows[:-3])):
            return Screen("trust", ("Folder access", "Open restricted"))

    if footer == "enter continue · esc skip" and _numbered_menu(rows[-4:-1], (
            "1. Update now (runs `npm install -g @openai/codex`)", "2. Skip", "3. Skip until next version"),
            cursor="› ") and any(re.fullmatch(r"Update available · \d+\.\d+\.\d+ → \d+\.\d+\.\d+", row)
                                   for row in rows[:-4]):
        return Screen("other", ("Update available", footer))

    login = "Choose how you want to use Codex."
    if footer == "Press enter to continue" and login in rows[:-1]:
        options = [row for row in rows[rows.index(login) + 1:-1] if re.match(r"^(?:> )?\d+\. ", row)]
        if _numbered_menu(options, ("1. Sign in with ChatGPT", "2. Sign in with Device Code",
                                   "3. Sign in another way"), cursor="> "):
            return Screen("login", (login, footer))

    model = "Select Model and Effort"
    if footer == "enter select · esc back" and model in rows[:-1]:
        options = [row for row in rows[rows.index(model) + 1:-1] if row not in {"↑", "↓"}]
        if (len(options) >= 2 and sum(row.startswith("› ") for row in options) == 1
                and all(re.fullmatch(r"(?:› )?[1-9]\d*\. \S.*", row) for row in options)):
            return Screen("other", (model, footer))
    return None


def _claude_choice(rows):
    footer = rows[-1]
    if footer == "Enter to confirm · Esc to cancel" and any(
            row.startswith("Quick safety check:") for row in rows[:-3]):
        options = rows[-3:-1]
        if (sum(row.startswith("❯ ") for row in options) == 1
                and {row.removeprefix("❯ ") for row in options} == {"No, exit", "Yes, I trust this folder"}):
            return Screen("trust", ("Quick safety check", footer))

    if (footer == "Esc to cancel" and len(rows) >= 6 and rows[-5] == "Select login method:"
            and "Login" in rows[-12:-5]):
        options = rows[-4:-1]
        prefixes = ("1. Claude account with subscription · ", "2. Anthropic Console account · ",
                    "3. 3rd-party platform · ")
        if (sum(row.startswith("❯ ") for row in options) == 1
                and all(row.removeprefix("❯ ").startswith(prefix) for row, prefix in zip(options, prefixes))):
            return Screen("login", ("Select login method:", footer))

    if (footer == "s to use this session only · Enter to save · Esc to cancel"
            and "Select model" in rows[:-1]
            and any(re.match(r"^❯ [1-9]\d*\. ", row) for row in rows[:-1])):
        return Screen("other", ("Select model", footer))
    return None


def _hermes_choice(rows):
    if len(rows) >= 5 and "Help improve Hermes?" in rows[:-4]:
        choices = rows[-3:]
        if (" ".join(rows[-4].split()) == "↑↓ navigate ENTER/SPACE select ESC cancel"
                and sum(row.startswith("→ ") for row in choices) == 1
                and tuple(re.sub(r"^(?:→ )?\([○●]\) ", "", row) for row in choices) == (
                    "Collect and send to Nous", "Collect locally only", "No thanks")):
            return Screen("consent", ("Help improve Hermes?", "↑↓ navigate  ENTER/SPACE select  ESC cancel"))
    if rows[-3:] == ["It looks like Hermes isn't configured yet -- no API keys or providers found.",
                     "Run:  hermes setup", "Run setup now? [Y/n]"]:
        return Screen("login", ("Run setup now? [Y/n]",))

    if len(rows) >= 8 and rows[-2] == "❯" and rows[-3] == rows[-1] and re.fullmatch(r"─{8,}", rows[-1]):
        panel = rows[-18:-3]
        title = "cli.tui.picker_title_provider"
        if (re.fullmatch(r"╰─{8,}╯", panel[-1])
                and any(re.fullmatch(rf"╭─ {re.escape(title)} ─+╮", row) for row in panel)
                and any(row.startswith("│ ❯ ") for row in panel)
                and any(row.lstrip("│ ").removeprefix("❯ ").startswith("cli.shared.cancel ") for row in panel)):
            return Screen("other", (title,))
    return None


def _dsh_choice(rows):
    footer = "←/→ step · Enter act on this step / go next · Esc skip the guide"
    title = "Welcome to dsh-TUI"
    if (rows[-1] == footer and title + "  Step 1 of 4" in rows[:-1]
            and "❯ Check again" in rows[:-1]
            and "✗ No usable credential, so the check cannot start" in rows[:-1]):
        return Screen("login", (title, footer))

    controls = "Tab / Shift+Tab providers · ↑/↓ model · Enter select · Esc cancel"
    # The picker sits directly above the input box, on both welcome and chat.
    welcome = (len(rows) >= 8 and rows[-1].startswith("Codex · ") and rows[-2].startswith("Claude · ")
               and re.fullmatch(r"▸ DSH · dsh-core v\S+", rows[-3])
               and re.fullmatch(r"╭─{8,}╮", rows[-8]) and rows[-6] == "╰" + rows[-8][1:-1] + "╯"
               and rows[-7].startswith("│ ❯ Say something, or type / for commands…"))
    if welcome:
        panel = rows[-22:-8]
    else:
        panel = []
        for index in range(len(rows) - 4, max(-1, len(rows) - 8), -1):
            top, prompt, bottom = rows[index:index + 3]
            if (re.fullmatch(r"╭─{8,}╮", top) and bottom == "╰" + top[1:-1] + "╯"
                    and re.fullmatch(r"⌸\s+❯\s+⛶", prompt)
                    and re.search(r"\bctx \d+(?:\.\d+)?% \([^)]*\)$", rows[-1])):
                panel = rows[max(0, index - 14):index]
                break
    for index, row in enumerate(panel[:-1]):
        if (row == "Model" and re.fullmatch(re.escape(controls) + r"(?: {4,}\S.*)?", panel[index + 1])
                and "Reasoning effort  ←/→ adjust" in panel[index + 2:]
                and any(row.startswith("❯ ") for row in panel[index + 2:])):
            return Screen("other", ("Model", controls))
    return "clear" if welcome else None


def _agy_choice(rows):
    footer = "↑/↓ Navigate · enter Select"
    if (len(rows) >= 5 and rows[-1] == footer and rows[-4] == "Select login method:"
            and rows[-5] == "Welcome to the Antigravity CLI. You are currently not signed in."
            and _numbered_menu(rows[-3:-1], ("1. Google OAuth", "2. Use a Google Cloud project"), cursor="> ")):
        return Screen("login", ("Select login method:", footer))
    return None


_CHOICE = {"codex": _codex_choice, "claude": _claude_choice, "dsh": _dsh_choice,
           "hermes": _hermes_choice, "agy": _agy_choice}


def observe(text, program, activity):
    """Return a safe Screen, positive clear evidence, or unknown (None)."""
    lines = normalize(text).splitlines()
    rows = [line.strip() for line in lines if line.strip()][-45:]
    if not rows:
        return None
    choice = _CHOICE.get(program)
    if choice and (screen := choice(rows)):
        return screen
    if program == "hermes" and activity == "question" and (question := parse_hermes_question(text)):
        return Screen("question", (question["control"],))
    if activity in {"wait", "work", "question"}:
        return "clear"
    if activity == "ask":
        if program == "dsh":
            title = "⏳ 等待审批" if any("⏳ 等待审批" in row for row in rows) else "Awaiting approval"
            return Screen("approval", (title,))
        if program == "hermes":
            hint = "↑/↓ で選択、Enter で確定"
            if not any(hint in row for row in rows[-10:]):
                hint = "↑/↓ to select, Enter to confirm"
            return Screen("approval", (hint,))
        # The display classifier is shared by Claude, Codex and Antigravity.
        # Bind notification to the harness's actual control, and require its
        # own current menu rather than a generic y/n or confirmation footer.
        if program == "codex":
            if rows[-1] == "enter confirm · esc skip" and current_modal_activity(lines) == "ask":
                return Screen("trust", ("Hooks need review", "enter confirm · esc skip"))
            options = rows[-5:-1]
            title = "Would you like to run the following command?"
            if (rows[-1] == _CODEX_CONFIRM and title in rows[:-1]
                    and "1. Yes, proceed (y)" in [row.removeprefix("› ") for row in options]
                    and any(re.match(r"^› [1-9]\d*\. ", row) for row in options)):
                return Screen("approval", (title, _CODEX_CONFIRM))
        if program == "claude":
            if rows[-1] in _CLAUDE_APPROVAL_FOOTERS and any(
                    re.match(r"^❯\s+[1-9]\d*\. ", row) for row in rows[-12:-1]):
                return Screen("approval", (rows[-1],))
            if (rows[-1].endswith("Enter to confirm · Esc to cancel")
                    and any(row.startswith("Computer use needs macOS permissions") for row in rows[-32:])):
                return Screen("approval", ("Computer use needs macOS permissions",))
        if program == "agy" and len(rows) >= 3:
            if rows[-2] == AGY_MCP_APPROVAL_CONTROLS:
                return Screen("approval", ("Allow calling this tool?", AGY_MCP_APPROVAL_CONTROLS))
            if rows[-2].startswith("↑/↓ Navigate · tab Amend"):
                return Screen("approval", ("Run this command?",))
    if program == "agy" and len(rows) >= 4 and rows[-1].startswith("? for shortcuts"):
        if (rows[-4] == rows[-2] and re.fullmatch(r"[─━]{8,}", rows[-2])
                and re.fullmatch(r">(?:\s.*)?", rows[-3])):
            return "clear"
    return None


class Monitor:
    """Pending timers live in memory. A private journal claims each Mail before send."""

    def __init__(self, path, send):
        self.path = path
        self.send = send
        self.pending = {}

    def tick(self, samples):
        ready = []
        pending = {}
        for sample in samples:
            if not sample["parent"]:
                continue
            key = json.dumps([sample["project_key"], sample["agent_id"], sample["generation"], sample["parent"]])
            evidence, capture = sample["human_wait"], sample["capture_id"]
            previous_sample = self.pending.get(key)
            if previous_sample and capture < previous_sample[2]:
                pending[key] = previous_sample
                continue
            if evidence == "clear":
                ready.append((key, sample, None, capture))
            if not isinstance(evidence, Screen):
                continue
            screen, first, last = self.pending.get(key, (evidence, capture, capture))
            if evidence != screen or capture - last > WAIT_SECONDS:
                first = capture
            pending[key] = (evidence, first, max(last, capture))
            if capture - first >= WAIT_SECONDS:
                ready.append((key, sample, [evidence.kind, *evidence.lines], first))
        self.pending = pending
        if not ready or (not os.path.lexists(self.path) and all(screen is None for _, _, screen, _ in ready)):
            return

        claimed = []
        with locked_events(self.path) as (events, previous):
            for key, sample, screen, first in ready:
                capture = sample["capture_id"]
                old = events.get(key)
                if old is None and screen is None:
                    continue
                if old is not None and (capture <= old["capture"] or old["screen"] == screen):
                    continue
                if old is not None and old["screen"] is None and first <= old["capture"]:
                    # Another monitor saw the screen clear. Restart our timer
                    # from our first observation after that recorded boundary.
                    self.pending[key] = (sample["human_wait"], capture, capture)
                    continue
                events[key] = {"screen": screen, "capture": capture}
                if screen is not None:
                    claimed.append(sample)
            save_events(self.path, events, previous)

        for sample in claimed:
            try:
                status = self.send(sample, sample["human_wait"])
            except Exception:
                status = "delivery_unknown"
            if status != "sent":
                logging.warning("Human-wait Mail for %s: %s", sample["name"], status)
