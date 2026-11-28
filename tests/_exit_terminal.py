"""Synthetic terminal observations for the actual exit submission protocol."""
import os
import subprocess


class ExitTerminal:
    def __init__(self, monkeypatch, server, name, *, editor="codex", pane_command=None,
                 screen=None, after_paste=None, after_interrupt=None, created=17, pid=19,
                 legacy_metadata=False, fail_capture=False):
        self.name = name
        self.editor = editor
        self.pane_command = pane_command or editor
        self.input = ""
        self.screen = screen
        self.after_paste = after_paste
        self.after_interrupt = after_interrupt
        self.created = created
        self.pid = pid
        self.legacy_metadata = legacy_metadata
        self.fail_capture = fail_capture
        self.mutations = []
        self.loaded = None
        tmux = server._MODE_API["runtime_tmux_api"]()
        submission = tmux["module_api"]("pane_submission")
        monkeypatch.setitem(submission["submit"].__globals__, "try_lock", lambda _: os.open(os.devnull, os.O_RDONLY))
        monkeypatch.setattr(server, "_has_session", lambda _: True)
        monkeypatch.setattr(server, "_tmux_run", self.command)
        self.metadata = submission["METADATA"]

    def render(self):
        if self.screen is not None:
            return self.screen
        draft = self.input
        border = "─" * 44
        if self.editor == "agy":
            return f"{border}\n> {draft}\n{border}\nauto · Gemini 4 Neon · high\n", 1, 2 + len(draft)
        if self.editor == "claude":
            return f"{border}\n❯ {draft}\n{border}\n", 1, 2 + len(draft)
        if self.editor == "dsh":
            return f"  ╭{border}╮\n  ⌸ ❯ {draft}\n  ╰{border}╯\n", 1, 6 + len(draft)
        if self.editor == "hermes":
            return f"☤ Hermes\n{border}\nHermes ❯ {draft}\n{border}\n", 2, 9 + len(draft)
        return f"› {draft}\n? for shortcuts\n", 0, 2 + len(draft)

    def command(self, args, **kwargs):
        stdout = ""
        if args[0] == "display-message":
            if len(args) > 4 and args[4].startswith(self.metadata):
                if self.fail_capture:
                    return subprocess.CompletedProcess(args, 1, "", "")
                screen, row, col = self.render()
                values = {
                    "socket_path": "/tmp/synthetic-tmux", "pid": self.pid, "pane_id": "%41", "pane_pid": 400,
                    "cursor_y": row, "pane_in_mode": 0, "cursor_x": col, "bracket_paste_flag": 1,
                    "pane_current_command": self.pane_command, "@orrery-agy-mcp-preparing": "",
                    "pane_width": 80, "pane_height": 24,
                }
                keys = [field.removeprefix("#{").removesuffix("}") for field in args[4].split("\t")]
                if self.legacy_metadata:
                    keys.remove("@orrery-agy-mcp-preparing")
                stdout = "\t".join(str(values[key]) for key in keys) + "\n" + screen
            elif args[-1] == "#{pane_current_command}":
                stdout = self.pane_command
            elif args[-1] == "#{pane_pid}":
                stdout = "400"
            else:
                stdout = f"{self.name}|{self.created}|{self.pid}|%41"
        elif args[0] == "load-buffer":
            self.loaded = kwargs["input"]
        elif args[0] == "paste-buffer":
            self.input = self.loaded
            self.screen = self.after_paste
            self.mutations.append(("paste", self.input))
        elif args[0] == "send-keys":
            self.mutations.append(tuple(args))
            if args[-1] == "C-m":
                self.input = ""
            elif args[-1] in {"C-c", "Escape"}:
                self.screen = self.after_interrupt
        elif args[0] != "delete-buffer":
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, stdout, "")
