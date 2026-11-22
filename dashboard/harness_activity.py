"""Observe native activity from the current pane footer, without I/O.

Pinned formats: dsh-tui 0.16.1 ApprovalPanel/AskUserQuestionPanel/StatusLine;
Hermes d923a5eb cli_tui_mixin and bundled en/ja catalogs. These are display
observations, not authenticated events. Missing evidence stays unknown.
"""
import re

try:
    from .harness_context import parse_context
except ImportError:  # executable dashboard/server.py
    from harness_context import parse_context

_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
_EDGES = ' \t\xa0\u3000│┃╭╮╰╯┌┐└┘─━┏┓┗┛'
_DSH_APPROVAL = re.compile(r'(?:Awaiting approval · .+|⏳ 等待审批 · .+)')
_DSH_QUESTION = re.compile(r'(?:[▸▾]\s*)?📋 (?:Question \d+/\d+.*|提问 · 第 \d+/\d+ 题.*)')
_DSH_CONFIRM = re.compile(
    r'↑/↓(?:/Tab)? (?:select · Enter confirm · (?:on No, type a reason · )?Esc reject'
    r'|选择 · Enter 确认 · (?:选中拒绝后可输入理由 · )?Esc 拒绝)')
_DSH_ANSWER = re.compile(r'(?:Type answer|输入回答|↑/↓ (?:select|选择)) · .*Enter (?:submit|提交) · .*Esc (?:cancel|previous question|中断|上一题).*')
_DSH_FOLDED = re.compile(r'(?:⏸\s+)?(?:Waiting for your answer|正在等你回答) — .+ (?:to expand|展开)')
_DSH_OPERATION_START = re.compile(
    r'(?:↑/↓(?:/Tab)? (?:select|选择) ·|(?:Type answer|输入回答) ·'
    r'|(?:⏸\s+)?(?:Waiting for your answer|正在等你回答) —)')
_HERMES_APPROVAL = re.compile(r'⚠\ufe0f?\s+(?:Dangerous Command|危険なコマンド)')
_HERMES_QUESTION = re.compile(r'.+(?: needs your input| が入力を求めています)')
_COUNTDOWN = r'(?:\s+\(\d+s\))?'
_HERMES_CONFIRM = re.compile(
    r'(?:↑/↓ to select, Enter to confirm|↑/↓ で選択、Enter で確定)' + _COUNTDOWN)
_HERMES_ANSWER = re.compile(
    r'(?:type your answer and press Enter|↑/↓ to select, Enter to lock, Tab next question'
    r'|回答を入力して Enter を押してください|↑/↓ で選択、Enter で確定、Tab で次の質問)' + _COUNTDOWN)
_HERMES_BUSY = re.compile(r'☤(?:\s+❯)?(?:\s.*)?')
_HERMES_IDLE = re.compile(r'(?:[\w.-]+\s+)?❯(?:\s.*)?')
_HERMES_APPROVAL_PROMPT = re.compile(r'⚠\ufe0f?(?:\s+❯)?(?:\s.*)?')
_HERMES_QUESTION_PROMPT = re.compile(r'[?✎](?:\s+❯)?(?:\s.*)?')
_DSH_DURATION = r'(?:\d+h(?: \d+m)?(?: \d+s)?|\d+m(?: \d+s)?|\d+s)'
_DSH_TOKEN_NUMBER = r'\d+(?:\.\d+)?[kKmMbB]?'
_DSH_TOKEN_PART = (r'[↑↓]\s+(?:↑ ' + _DSH_TOKEN_NUMBER + r' · )?↓ '
                   + _DSH_TOKEN_NUMBER + r' tokens')
_DSH_CLASSIC_BUSY = re.compile(
    r'[·•●]\s+[^\n()]+…\s+\(' + _DSH_DURATION
    + r'(?: · ' + _DSH_TOKEN_PART + r')?'
    + r'(?: · (?:thinking|thought for \d+s))?\)')
_DSH_METRICS = re.compile(
    r'(?:\d+(?:\.\d+)?[kKmM]?/\d+(?:\.\d+)?[kKmM]? \d+(?:\.\d+)?%'
    r'|·(?:\s+·)*'
    r'|sys\s+\S+\s+pr\s+\S+\s+ast\s+\S+\s+th\s+\S+\s+tl\s+\S+\s+free\s+\S+)')


def _dsh_extended_input(lines):
    """Observe the current composer plus bounded StatusLine hover metrics.

    Native Chat gates WorkingSpinner on channel.working. Only its adjacent
    classic form is recognized here; absent spinner is not evidence of idle.
    A recognized composer suppresses stale modal examples even when unknown.
    """
    rows = [line.strip() for line in lines if line.strip()]
    interrupts = ('esc to interrupt', 'esc 中断')
    shortcuts = ('? for shortcuts', '? 查看快捷键')
    for index in reversed(range(max(0, len(rows) - 10), len(rows) - 2)):
        top, prompt, bottom = rows[index:index + 3]
        if not (re.fullmatch(r'╭─{8,}╮', top)
                and bottom == '╰' + top[1:-1] + '╯'
                and re.fullmatch(r'⌸\s+❯.*\s+⛶', prompt)):
            continue
        tail = rows[index + 3:]
        if not 1 <= len(tail) <= 6:
            return False, None
        contexts = [parse_context(row, 'dsh')['ctx_used'] is not None for row in tail]
        if not any(contexts) or not all(
            context or _DSH_METRICS.fullmatch(row) or row in interrupts + shortcuts
            for row, context in zip(tail, contexts)
        ):
            return False, None
        if tail[-1] in interrupts:
            return True, 'work'
        if tail[-1] in shortcuts:
            return True, 'wait'
        if index and _DSH_CLASSIC_BUSY.fullmatch(rows[index - 1]):
            return True, 'work'
        return True, None
    return False, None


def _hermes_prompt(lines):
    rows = [line.strip() for line in lines if line.strip()]
    if len(rows) >= 3:
        top, prompt, bottom = rows[-3:]
        if top == bottom and re.fullmatch(r'─{8,}', top):
            return prompt
    return None


def _input_activity(lines, program):
    """Recognize complete current input chrome, never arbitrary transcript text.

    Hermes cli_tui_mixin:272–346,2143–2161: state icon replaces profile
    prompt; rules surround the input. dsh PromptInput:59,3810–3841 and
    StatusLine:370–376: same input box, extra interrupt hint only while busy.
    """
    rows = [line.strip() for line in lines if line.strip()]
    if program == 'hermes':
        prompt = _hermes_prompt(lines)
        if prompt is not None and _HERMES_BUSY.fullmatch(prompt):
            return 'work'
        if prompt is not None and _HERMES_IDLE.fullmatch(prompt):
            return 'wait'
    interrupt = program == 'dsh' and rows and rows[-1] in ('esc to interrupt', 'esc 中断')
    if interrupt:
        rows = rows[:-1]
    if program == 'dsh' and rows and parse_context(rows[-1], 'dsh')['ctx_used'] is not None:
        # Footer has a context status and optionally the compact occupancy bar.
        for index in range(max(0, len(rows) - 5), len(rows) - 2):
            top, prompt, bottom = rows[index:index + 3]
            if (re.fullmatch(r'╭─{8,}╮', top)
                    and bottom == '╰' + top[1:-1] + '╯'
                    and re.fullmatch(r'⌸\s+❯.*\s+⛶', prompt)
                    and 1 <= len(rows) - index - 3 <= 2):
                return 'work' if interrupt else 'wait'
    return None


def parse_activity(text, program):
    """Return work/question/ask/wait or None; absence never implies idle or death.

    dsh needs its title and a current operation footer. Hermes instead pairs
    its current modal prompt with the operation footer, even if a long command
    pushes the title offscreen. The footer must be near the bottom.
    Native turns remain working while awaiting a modal. A visible ordinary
    composer contradicts a modal: dsh hides it; Hermes changes its prompt icon.
    """
    if program not in ('dsh', 'hermes') or not isinstance(text, str):
        return None
    lines = _ANSI.sub('', text).replace('\r', '').splitlines()[-45:]
    rows = [row.strip(_EDGES) for row in lines]
    rows = [row for row in rows if row]
    if not rows:
        return None
    current_input = _input_activity(lines, program)
    if current_input is not None:
        # Panel-like text above an ordinary current input is transcript output.
        return current_input
    if program == 'dsh':
        observed_input, activity = _dsh_extended_input(lines)
        if observed_input:
            return activity
    footer = rows[-6:]
    titles = rows[-36:]

    def operations(hint):
        # Text widgets can wrap their operation hints at word boundaries.
        for index in range(len(footer)):
            for end in range(index + 1, len(footer) + 1):
                if hint.fullmatch(' '.join(footer[index:end])):
                    yield index

    def modal(title, hint):
        # Require the title before the matching operation, not a later quoted
        # title after an old footer. Wide/long panels outside this window are unknown.
        return any(any(title.fullmatch(previous)
                   for previous in titles[:len(titles) - len(footer) + index])
                   for index in operations(hint))

    if program == 'dsh':
        if modal(_DSH_APPROVAL, _DSH_CONFIRM):
            return 'ask'
        if modal(_DSH_QUESTION, _DSH_ANSWER) or modal(_DSH_QUESTION, _DSH_FOLDED):
            return 'question'
        if any(_DSH_APPROVAL.fullmatch(row) or _DSH_QUESTION.fullmatch(row) for row in titles):
            return None
        if any(_DSH_OPERATION_START.match(row) for row in footer):
            return None
        if rows[-1] in ('esc to interrupt', 'esc 中断'):
            return 'work'
    else:
        prompt = _hermes_prompt(lines) or ''
        if _HERMES_APPROVAL_PROMPT.fullmatch(prompt) and next(operations(_HERMES_CONFIRM), None) is not None:
            return 'ask'
        if _HERMES_QUESTION_PROMPT.fullmatch(prompt) and next(operations(_HERMES_ANSWER), None) is not None:
            return 'question'
        if any(_HERMES_APPROVAL.fullmatch(row) or _HERMES_QUESTION.fullmatch(row) for row in titles):
            return None
    return None
