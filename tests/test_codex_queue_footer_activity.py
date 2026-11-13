"""Current Codex work survives its queued-input UI and model display footer."""

from __future__ import annotations

from time import perf_counter
from types import SimpleNamespace

import pytest

import dashboard.server as server


_WORK = "• Working (26m 32s • esc to interrupt)\n"
_TRUNCATED = (
    "• Working (26m 32s • esc to interrupt) · 1 background terminal running · /ps to…\n"
)
_QUEUE = (
    "• Queued follow-up inputs\n"
    "  ↳ Continue the synthetic task\n"
    "    ⇧← edit last queued message\n"
)
_ASK = (
    "Would you like to run this command?\n"
    "› 1. Yes\n"
    "  2. No\n"
    "Press enter to confirm or esc to cancel\n"
)
_QUESTION = "Choose a result\n❯ 1. Option\nEnter to select · Esc to cancel\n"


def _composer(model="GPT-6.1-Sol", *, context=False, prompt="›"):
    ctx = "Context 70% left · " if context else ""
    return (
        f"\n{prompt} Ask Codex to do anything\n\n"
        f"  {model} max · {ctx}~/project · task\n"
        "  ← for agents · ? for shortcuts\n"
    )


def _activity(parsed):
    return parsed["act_state"], parsed["work_secs"], parsed["work_disp"]


@pytest.mark.parametrize("status,queue,model,expected_model", [
    pytest.param(_WORK, _QUEUE, "GPT-6.1-Sol", "GPT-6.1-Sol", id="issue-queue"),
    pytest.param(_WORK, "", "GPT-6.1 Terra", "GPT-6.1-Terra", id="issue-space-footer"),
    pytest.param(_TRUNCATED, "", "GPT-6.1 Terra", "GPT-6.1-Terra", id="issue-truncated-space"),
    pytest.param(_TRUNCATED, _QUEUE, "GPT-6.1 Terra", "GPT-6.1-Terra", id="queue-and-space-footer"),
])
@pytest.mark.parametrize("padding", ["", "   "], ids=["plain", "joined-capture-spaces"])
def test_issue_work_reproductions(status, queue, model, expected_model, padding):
    text = status + "\n" + queue + _composer(model)
    text = "\n".join(row + padding for row in text.splitlines())
    parsed = server._parse_runtime(text)
    assert _activity(parsed) == ("work", 1592, "26m32s")
    assert parsed["pane_model"] == expected_model
    assert parsed["ctx_used"] is None


@pytest.mark.parametrize("queue", [
    pytest.param(_QUEUE, id="single"),
    pytest.param(
        "• Queued follow-up inputs\n"
        "  ↳ First synthetic request\n"
        "  ↳ Second synthetic request\n"
        "    ⇧← edit last queued message\n", id="multiple"),
    pytest.param(
        "• Queued follow-up inputs\n"
        "  ↳ A synthetic request containing a list\n"
        "    • A wrapped bullet in the queued request\n"
        "    ⇧← edit last queued message\n", id="body-bullet"),
    pytest.param(
        "• Queued follow-up inputs\n"
        "  ↳ A longer synthetic request that wraps\n"
        "    onto a continuation line\n"
        "      and another indented line\n"
        "    …\n"
        "  ↳ Another synthetic request\n"
        "    with a wrapped continuation\n"
        "    ⇧← edit last queued message\n", id="wrapped-and-ellipsized"),
])
@pytest.mark.parametrize("context", [False, True], ids=["without-context", "with-context"])
@pytest.mark.parametrize("tip", ["", "  └ Tip: Use a synthetic shortcut.\n"], ids=["no-tip", "tip"])
def test_complete_queue_preserves_current_work(queue, context, tip):
    parsed = server._parse_runtime(_WORK + tip + "\n" + queue + _composer("GPT-6.1 Terra", context=context))
    assert _activity(parsed) == ("work", 1592, "26m32s")
    assert parsed["pane_model"] == "GPT-6.1-Terra"
    assert parsed["ctx_used"] == (30 if context else None)


@pytest.mark.parametrize("status,seconds,display", [
    ("• Working (1h 2m 3s • esc to interrupt)", 3723, "1h02m"),
    ("• Working (26m 32s …", 1592, "26m32s"),
    ("• Working (26m 32s • esc to…", 1592, "26m32s"),
    ("▌ Thinking (1h 2m…", 3720, "1h02m"),
    ("● Working (1h 2m 3s • esc to inter…", 3723, "1h02m"),
    ("• Working (26m 3…", 1560, "26m"),
    ("• Working…", None, None),
])
def test_queued_work_keeps_only_readable_elapsed_units(status, seconds, display):
    parsed = server._parse_runtime(status + "\n\n" + _QUEUE + _composer("GPT-6.1 Terra"))
    assert _activity(parsed) == ("work", seconds, display)


@pytest.mark.parametrize("current", [
    pytest.param(_QUEUE, id="queue-without-spinner"),
    pytest.param(_WORK + "\n" + _QUEUE + "\n• Finished the task.\n", id="reply-after-queue"),
    pytest.param(_WORK + "\n• Finished the task.\n\n" + _QUEUE, id="old-work-before-reply-and-queue"),
    pytest.param("> " + _WORK + "\n" + _QUEUE, id="quoted-work"),
    pytest.param("  " + _WORK + "\n" + _QUEUE, id="indented-complete-work"),
    pytest.param("  • Working (26m 32s • esc to…\n\n" + _QUEUE, id="indented-narrow-work"),
    pytest.param("```text\n" + _WORK + "```\n\n" + _QUEUE, id="fenced-work"),
])
def test_queue_does_not_revive_noncurrent_or_quoted_work(current):
    assert _activity(server._parse_runtime(current + _composer())) == ("wait", None, None)


@pytest.mark.parametrize("queue", [
    pytest.param("• Queued follow-up inputs\n  ↳ Synthetic request\n", id="missing-hint"),
    pytest.param("• Queued follow-up inputs\n    ⇧← edit last queued message\n", id="missing-item"),
    pytest.param(_QUEUE.replace("  ↳ Continue the synthetic task", "  ↳ "), id="empty-item"),
    pytest.param(" " + _QUEUE, id="indented-heading"),
    pytest.param(_QUEUE.replace("inputs", "input"), id="different-heading"),
    pytest.param(_QUEUE.replace("  ↳", "\n  ↳"), id="blank-before-item"),
    pytest.param(_QUEUE.replace("    ⇧←", "\n    ⇧←"), id="blank-before-hint"),
    pytest.param(_QUEUE.replace("    ⇧←", "unindented output\n    ⇧←"), id="column-zero-inside"),
    pytest.param(_QUEUE.replace("    ⇧←", "• Another reply\n    ⇧←"), id="other-bullet-inside"),
    pytest.param(_QUEUE.replace("    ⇧←", "   short indent\n    ⇧←"), id="short-continuation"),
    pytest.param(_QUEUE.replace("  ↳", "   ↳"), id="wrong-item-indent"),
    pytest.param(_QUEUE.replace("    ⇧←", "   ⇧←"), id="wrong-hint-indent"),
    pytest.param(_QUEUE.replace("edit last queued message", "edit queued message"), id="different-hint"),
])
def test_incomplete_queue_is_a_work_boundary(queue):
    assert _activity(server._parse_runtime(_WORK + "\n" + queue + _composer())) == ("wait", None, None)


@pytest.mark.parametrize("footer", [
    _composer(prompt="❯"),
    "\n❯\nfixture | Sonnet 5 | ctx: 30% used\n",
    "\n› Ask Codex to do anything\n",
])
def test_queue_requires_codex_composer(footer):
    assert _activity(server._parse_runtime(_WORK + "\n" + _QUEUE + footer)) == ("wait", None, None)


@pytest.mark.parametrize("control,state", [(_ASK, "ask"), (_QUESTION, "question")])
def test_queue_does_not_override_current_controls(control, state):
    assert _activity(server._parse_runtime(_WORK + "\n" + _QUEUE + "\n" + control)) == (state, None, None)


@pytest.mark.parametrize("footer", [
    "GPT-6.1 Terra Mini max · ~/project",
    "GPT-6.1 Terra · ~/project",
    "GPT-6.1 Terra unknown · ~/project",
    "Other-6.1 Terra max · ~/project",
    "GPT-6.1 max high · ~/project",
    "GPT-6.1  Terra max · ~/project",
])
@pytest.mark.parametrize("queue", ["", _QUEUE], ids=["no-queue", "queue"])
def test_unknown_footer_does_not_admit_current_work(footer, queue):
    text = _WORK + "\n" + queue + "\n› Ask Codex to do anything\n\n  " + footer + "\n"
    parsed = server._parse_runtime(text)
    assert _activity(parsed) == ("wait", None, None)
    assert parsed["pane_model"] is None


@pytest.mark.parametrize("variant", ["Sol", "Terra", "NovaXL", "nova-preview2", "custom_2"])
@pytest.mark.parametrize("separator", ["·", "•"])
def test_space_variant_model_is_normalized_without_a_name_allowlist(variant, separator):
    text = f"›\n  GPT-6.1 {variant} max {separator} ~/project\n"
    assert server._parse_runtime(text)["pane_model"] == f"GPT-6.1-{variant}"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max", "ultra"])
@pytest.mark.parametrize("variant", ["", " Terra"])
def test_effort_is_not_a_model_variant(effort, variant):
    text = f"›\n  GPT-6.1{variant} {effort} · ~/project\n"
    expected = "GPT-6.1-Terra" if variant else "GPT-6.1"
    assert server._parse_runtime(text)["pane_model"] == expected


@pytest.mark.parametrize("footer,model,context", [
    ("GPT-6.1 max · ~/project", "GPT-6.1", None),
    ("gpt-5.5 medium · Context 46% left", "gpt-5.5", 54),
    ("GPT-6.1 Terra max · Context 70% left · ~/project", "GPT-6.1-Terra", 30),
    ("gpt-5.6-terra medium · ~/project", "gpt-5.6-terra", None),
])
def test_footer_model_and_context(footer, model, context):
    parsed = server._parse_runtime("›\n  " + footer + "\n")
    assert parsed["pane_model"] == model
    assert parsed["ctx_used"] == context


@pytest.mark.parametrize("body", [
    "Compare GPT-6.1 Terra and GPT-6.1-Sol later.",
    "• The selected model is GPT-6.1 Terra max.",
    "The example footer says GPT-6.1 Terra max · ~/project.",
])
def test_conversation_model_names_are_not_statuslines(body):
    assert server._parse_runtime(body + "\n›\n")["pane_model"] is None


def test_runtime_clears_queued_work_time_after_current_status_finishes(monkeypatch):
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
    assert _activity(first) == ("work", 1592, "26m32s")
    second = server._agent_runtime("FixtureAgent", session_created=101, session_id="$1")
    assert _activity(second) == ("wait", None, None)


@pytest.mark.parametrize("text,expected", [
    pytest.param(
        _WORK + "\n• Queued follow-up inputs\n  ↳ " + "x" * 16000
        + "\n    " + " " * 16000 + "…\n    ⇧← edit last queued message\n"
        + _composer("GPT-6.1 Terra"), ("work", 1592, "26m32s"), id="long-valid-queue"),
    pytest.param(
        _WORK + "\n" + _QUEUE + "\n›\n  gpt-" + "x" * 16000
        + " " * 16000 + "Terra unknown · ~/project\n", ("wait", None, None), id="long-invalid-footer"),
    pytest.param(
        _WORK + "\n" + _QUEUE.replace("    ⇧←", "   " + "x" * 16000 + "\n    ⇧←")
        + _composer(), ("wait", None, None), id="long-invalid-queue"),
])
def test_long_footer_and_queue_lines_have_bounded_parse_time(text, expected):
    start = perf_counter()
    parsed = server._parse_runtime(text)
    duration = perf_counter() - start
    assert _activity(parsed) == expected
    assert duration < 0.25, f"pathological footer/queue took {duration:.3f}s"
