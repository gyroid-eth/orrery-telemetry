"""tmux 3.4 (Ubuntu 24.04) escapes the 0x1f field separator in `-F` output.

Every row came back with the four characters ``\\037`` where macOS tmux 3.6a
and WSL2 tmux emit the raw control character, so ``line.split(SEP)`` returned
one field per row and the dashboard listed every live agent as GONE (#26).
Both spellings must parse to the same session table.
"""
from dashboard import server


def _rows(sep):
    def fake_tmux(args):
        if args[0] == "list-sessions":
            return sep.join(("LiveAgent", "1700000000", "1700000100", "$3")) + "\n"
        if args[0] == "list-panes":
            return sep.join(("LiveAgent", "11", "zsh", "4321", "✳ working")) + "\n"
        if args[0] == "list-clients":
            return sep.join(("LiveAgent", "/dev/pts/2")) + "\n"
        return ""
    return fake_tmux


def _state(monkeypatch, sep):
    monkeypatch.setattr(server, "_tmux", _rows(sep))
    monkeypatch.setattr(server, "_prune_runtime_cache", lambda _sessions: None)
    return server.tmux_state()


def test_raw_control_separator_still_parses(monkeypatch):
    state = _state(monkeypatch, server.SEP)
    assert set(state) == {"LiveAgent"}
    row = state["LiveAgent"]
    assert (row["created"], row["activity"], row["session_id"]) == (1700000000, 1700000100, "$3")
    assert (row["cmd"], row["pane_pid"], row["title"]) == ("zsh", 4321, "✳ working")
    assert row["attached"] is True and row["client_tty"] == "/dev/pts/2"


def test_escaped_separator_from_tmux_34_parses_identically(monkeypatch):
    raw = _state(monkeypatch, server.SEP)
    escaped = _state(monkeypatch, "\\037")
    assert escaped == raw


def test_split_helper_keeps_title_intact_after_maxsplit():
    line = "\\037".join(("A", "11", "zsh", "9", "title with \\037 inside"))
    parts = server._split_tmux_fields(line, 4)
    assert parts[:4] == ["A", "11", "zsh", "9"]
    assert parts[4] == "title with " + server.SEP + " inside"
