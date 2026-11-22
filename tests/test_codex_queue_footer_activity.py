"""Codex work-state regressions for queued inputs and model footers."""

from __future__ import annotations

from time import perf_counter
from types import SimpleNamespace

import pytest

import dashboard.server as server


_WORK = "• Working (26m 32s • esc to interrupt)\n"
_TRUNCATED = "• Working (26m 32s • esc to interrupt) · 1 background terminal running · /ps to…\n"
_QUEUE = (
    "• Queued follow-up inputs\n"
    "  ↳ Continue the synthetic task\n"
    "    ⇧← edit last queued message\n"
)


def _composer(model="GPT-6.1-Sol", *, context=False):
    usage = "Context 70% left · " if context else ""
    return (
        "\n› Ask Codex to do anything\n\n"
        f"  {model} max · {usage}~/project · task\n"
        "  ← for agents · ? for shortcuts\n"
    )


def _activity(parsed):
    return parsed["act_state"], parsed["work_secs"], parsed["work_disp"]


def test_queued_input_keeps_live_work():
    parsed = server._parse_runtime(_WORK + "\n" + _QUEUE + _composer())
    assert _activity(parsed) == ("work", 1592, "26m32s")
    assert parsed["pane_model"] == "GPT-6.1-Sol"


def test_space_separated_model_footer_keeps_work_and_context():
    parsed = server._parse_runtime(_WORK + _composer("GPT-6.1 Terra", context=True))
    assert _activity(parsed) == ("work", 1592, "26m32s")
    assert parsed["pane_model"] == "GPT-6.1-Terra"
    assert parsed["ctx_used"] == 30


def test_truncated_work_above_queue_is_live():
    parsed = server._parse_runtime(_TRUNCATED + "\n" + _QUEUE + _composer("GPT-6.1 Terra"))
    assert _activity(parsed) == ("work", 1592, "26m32s")


def test_wrapped_multi_item_queue_preserves_work():
    queue = (
        "• Queued follow-up inputs\n"
        "  ↳ First synthetic request\n"
        "    • Wrapped bullet\n"
        "    …\n"
        "  ↳ Another synthetic request\n"
        "    ⇧← edit last queued message\n"
    )
    assert _activity(server._parse_runtime(_WORK + "\n" + queue + _composer())) == (
        "work", 1592, "26m32s"
    )


def test_queue_without_spinner_is_waiting():
    assert _activity(server._parse_runtime(_QUEUE + _composer())) == ("wait", None, None)


def test_reply_after_queue_is_waiting():
    text = _WORK + "\n" + _QUEUE + "\n• Finished the task.\n" + _composer()
    assert _activity(server._parse_runtime(text)) == ("wait", None, None)


def test_quoted_work_is_not_revived_by_queue():
    text = "  " + _WORK + "\n" + _QUEUE + _composer()
    assert _activity(server._parse_runtime(text)) == ("wait", None, None)


def test_incomplete_queue_does_not_expose_old_work():
    queue = _QUEUE.replace("edit last queued message", "edit queued message")
    assert _activity(server._parse_runtime(_WORK + "\n" + queue + _composer())) == (
        "wait", None, None
    )


def test_claude_composer_cannot_remove_codex_queue():
    text = _WORK + "\n" + _QUEUE + "\n❯\nfixture | Sonnet 5 | ctx: 30% used\n"
    assert _activity(server._parse_runtime(text)) == ("wait", None, None)


def test_current_approval_overrides_queued_work():
    approval = (
        "Would you like to run this command?\n"
        "› 1. Yes\n"
        "  2. No\n"
        "Press enter to confirm or esc to cancel\n"
    )
    parsed = server._parse_runtime(_WORK + "\n" + _QUEUE + "\n" + approval)
    assert _activity(parsed) == ("ask", None, None)


def test_current_question_overrides_queued_work():
    question = "Choose a result\n❯ 1. Option\nEnter to select · Esc to cancel\n"
    parsed = server._parse_runtime(_WORK + "\n" + _QUEUE + "\n" + question)
    assert _activity(parsed) == ("question", None, None)


def test_unknown_footer_is_not_a_composer_statusline():
    text = (
        _WORK + "\n" + _QUEUE
        + "\n› Ask Codex to do anything\n\n  GPT-6.1 Terra unknown · ~/project\n"
    )
    parsed = server._parse_runtime(text)
    assert _activity(parsed) == ("wait", None, None)
    assert parsed["pane_model"] is None


def test_variant_without_allowlist_and_effort_without_variant():
    variant = server._parse_runtime("›\n  GPT-6.1 custom_2 max · ~/project\n")
    unsuffixed = server._parse_runtime("›\n  GPT-6.1 max · ~/project\n")
    assert variant["pane_model"] == "GPT-6.1-custom_2"
    assert unsuffixed["pane_model"] == "GPT-6.1"


def test_existing_hyphenated_footer_and_context():
    parsed = server._parse_runtime("›\n  gpt-5.6-terra medium · Context 46% left\n")
    assert parsed["pane_model"] == "gpt-5.6-terra"
    assert parsed["ctx_used"] == 54


@pytest.mark.parametrize("draft_prompt", ["› Ask Codex to do anything", "❯"])
def test_unsent_draft_does_not_count_as_work_or_override_model(draft_prompt):
    text = (
        "■ Selected model is at capacity. Please try a different model.\n\n"
        "› Repro:\n"
        "  • Working (26m 32s • esc to interrupt)\n"
        f"  {draft_prompt}\n"
        "  GPT-6.1 Terra max · ~/snippet\n\n"
        "  GPT-6.1-Sol max · ~/project · task\n"
    )
    parsed = server._parse_runtime(text)
    assert _activity(parsed) == ("wait", None, None)
    assert parsed["pane_model"] == "GPT-6.1-Sol"


def test_live_elapsed_time_disappears_after_reply(monkeypatch):
    active = _TRUNCATED + "\n" + _QUEUE
    footer = _composer("GPT-6.1 Terra")
    captures = iter([
        SimpleNamespace(returncode=0, stdout=active + footer),
        SimpleNamespace(returncode=0, stdout=active + "\n• Finished the task.\n" + footer),
    ])
    ticks = iter((0.0, 10.0))
    monkeypatch.setattr(server.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(server, "_tmux_run", lambda *args, **kwargs: next(captures))
    monkeypatch.setattr(server, "_rt_cache", {})
    monkeypatch.setattr(server, "_provider_captures", {})
    first = server._agent_runtime("FixtureAgent", session_created=101, session_id="$1")
    second = server._agent_runtime("FixtureAgent", session_created=101, session_id="$1")
    assert _activity(first) == ("work", 1592, "26m32s")
    assert _activity(second) == ("wait", None, None)


def test_long_invalid_footer_parses_in_bounded_time():
    text = (
        _WORK + "\n" + _QUEUE + "\n›\n  gpt-"
        + "x" * 16000 + " " * 16000 + "Terra unknown · ~/project\n"
    )
    start = perf_counter()
    parsed = server._parse_runtime(text)
    assert _activity(parsed) == ("wait", None, None)
    assert perf_counter() - start < 0.25
