"""Pure terminal evidence shared by observation and explicit compact admission."""
import re

_CTX_RE = re.compile(r"ctx:\s*(\d+)%\s*used")
_CTX_CODEX_LEFT_RE = re.compile(r"Context\s+(\d+)%\s*left", re.IGNORECASE)
_CTX_CODEX_USED_RE = re.compile(r"Context\s+(\d+)%\s*used", re.IGNORECASE)
# Native Codex 0.163.0 displays a model/effort row, optionally followed by cwd.
# Match complete rows; an unfamiliar effort or continuation is not evidence.
_CODEX_MODEL_FOOTER_RE = re.compile(
    r"gpt-\S+\s+(?:default|low|medium|high|xhigh|max|ultra)(?:\s+·\s+\S[^\n]*)?",
    re.IGNORECASE)
_CODEX_WARNING_FOOTER_RE = re.compile(r"⚠\s+\d+\s+warnings?\s+·\s+f2 to view")
_CODEX_SHORTCUT_FOOTER_RE = re.compile(
    r"\? for shortcuts(?:\s+⚠\s+\d+\s+warnings?\s+·\s+f2 to view)?")
_CODEX_CARD_MODEL = r"gpt-[a-z0-9]+(?:[._-][a-z0-9]+)*"
_CODEX_CARD_MODEL_RE = re.compile(
    rf"(?P<model>{_CODEX_CARD_MODEL}(?: [a-z0-9][a-z0-9_-]*)?)"
    r"\s+(?P<effort>default|none|minimal|low|medium|high|xhigh|max|ultra)"
    r"(?:\s+[·•]\s+\S[^\n]*)?", re.IGNORECASE)
_CODEX_CARD_MODEL_ONLY_RE = re.compile(
    rf"(?P<model>{_CODEX_CARD_MODEL})(?:\s+[·•]\s+Context\s+\d+%\s+(?:left|used))?",
    re.IGNORECASE)
_WORK_RE = re.compile(
    r"esc to interrupt"
    r"|·\s*[↑↓]?\s*[\d.]+k?\s*tokens"
    r"|\(\s*\d+m\s+\d+s\s*·"
)
_WORK_CLAUDE_TASK_RE = re.compile(
    r"(?m)"
    r"^[ \t\xa0]*[✢✳✶✻✽✺·][ \t\xa0]+"
    r"(?![^\n]*\bfor\s+(?:\d+h\s*)?(?:\d+m\s*)?\d+s\s*$)"
    r"\S(?:[^\n]*\S[^\n]*\n){0,2}[^\n]*…"
    r"|^[ \t\xa0]*[✢✳✶✻✽✺][ \t\xa0]*$")
_WORK_CODEX_RE = re.compile(
    r"(?m)^\s*[•▌●]\s*(?:Working|Thinking)\b[^\n]*\(\s*\d+\s*s\b")
_ASK_Q_RE = re.compile(r"Do you want to .+\?|Would you like to proceed")
_ASK_OPT_RE = re.compile(r"(?m)^\s*[❯>›]?\s*1\.\s")
_ASK_YN_RE = re.compile(r"\?\s*[\[(]y/n[\])]", re.IGNORECASE)
_ASK_CODEX_RE = re.compile(
    r"Press\s+enter\s+to\s+confirm\s+or\s+esc\s+to\s+cancel", re.IGNORECASE)
_QUESTION_RE = re.compile(r"Enter to select.{0,80}Esc to cancel")
_CLAUDE_APPROVAL_SCROLL_RE = re.compile(r"[↑↓] [1-9]\d* more lines?")
_AGY_COMMAND_APPROVAL_CONTROLS = "↑/↓ Navigate · tab Amend · ctrl+g edit/expand command"
_AGY_APPROVAL_FOOTER_RE = re.compile(r"esc to cancel(?:\s{2,}[a-z-]+ · [^·]+ · [a-z]+)?")
_AGY_COMMAND_OPTION_BREAK_RE = re.compile(r"\n(?=>? ?[1-9]\d*\. )")
_AGY_COMMAND_ALWAYS_RE = re.compile(
    r"[1-9]\d*\. Yes, and always allow(?: in this conversation)? "
    r"for commands that start with '.+'(?: \(Persist to settings\.json\))?"
)

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_STALL_WORK = re.compile(r"(?im)^\s*[•▌●]\s*(?:Working|Thinking)\b[^\n]*\([^\n]*(?:\d+\s*s|esc to interrupt)")

def normalize(text):
    return _ANSI.sub('', text).replace('\r','').replace('\xa0',' ')

_COMPACT_DURATION = re.compile(r'(?:[0-9]{1,4}h(?: [0-9]{1,3}m)?(?: [0-9]{1,2}s)?|[0-9]{1,3}m(?: [0-9]{1,2}s)?|[0-9]{1,2}s)')
_CLAUDE_COMPACT = re.compile(r'[✢✳✶✻✽·]\s+Compacting conversation… \(([^()]+)\)')
_CODEX_COMPACT = re.compile(r'[•▌●]\s+Compacting context\s*\(([0-9hms ]+)\s*[•·]\s*esc to interrupt\)')
_DSH_COMPACT = re.compile(
    r'\S{1,10}\s+(?:Summarizing earlier turns…|正在压缩会话…) · '
    r'(?:(?:reading context…|读取上下文…|↓ [\d.]+[kmb]? tokens) · )?'
    r'([^·]+?)(?: · (?:Esc cancels|Esc 取消))?')
_DSH_COMPACT_BUSY = re.compile(r'\S{1,10}\s+.+…\s*\((?:compacting|压缩中) · ([\dhms ]+)(?: · [^()]*)?\)')
_DSH_COMPACT_TOTAL = re.compile(r'\S{1,10}\s+.+(?:total |总)([\dhms ]+).* · (?:compacting|压缩中)')
_HERMES_COMPACT = re.compile(r'─\s+.+?\s+compacting…\s*·\s*([^│]+?)\s*│\s*\S.*')
_HERMES_COMPRESS = re.compile(
    r'─\s+(?:⠋ compressing \d+ messages \(~[\d,]+ tok\)|'
    r'🗜️ Compacting context — (?:still )?summarizing earlier conversation).*\s│\s+\S.*')
_COMPACT_RULE = re.compile(r'[─━═]{8,}(?: +[^─━═\s].*? +[─━═]+)?')


def _claude_compaction(lines):
    for input_at in range(len(lines) - 1, 0, -1):
        if re.match(r'\s*❯(?:\s|$)', lines[input_at]) and _COMPACT_RULE.fullmatch(lines[input_at - 1].strip()):
            break
    else:
        return None
    lower = next((i for i in range(input_at + 1, len(lines))
                  if _COMPACT_RULE.fullmatch(lines[i].strip())), None)
    if lower is None or not re.search(r'(?:^|·)\s*esc to interrupt\s*(?:·|$)', ' '.join(lines[lower + 1:]).strip()):
        return None
    block = [row for row in lines[:input_at - 1]
             if row.strip() and not re.fullmatch(r'\s+● \S+ · /effort', row)]
    for i in range(len(block) - 1, -1, -1):
        if re.match(r'\s+⎿\s+Tip:', block[i]) and all(re.match(r' {4,}\S', row) for row in block[i+1:]):
            block = block[:i]
            break
    return _CLAUDE_COMPACT.fullmatch(block[-1]) if block else None


def _codex_compaction(lines):
    for i in range(len(lines) - 1, -1, -1):
        if re.match(r'\s*›(?:\s|$)', lines[i]):
            break
    else:
        return None
    if not any(pattern.match(row.strip()) for row in lines[i+1:] for pattern in (
        _CODEX_MODEL_FOOTER_RE, _CODEX_SHORTCUT_FOOTER_RE, _CTX_CODEX_LEFT_RE, _CTX_CODEX_USED_RE,
    )):
        return None
    block = [row.strip() for row in lines[max(0, i - 5):i] if row.strip()]
    if block and block[-1] == 'Making room to continue.':
        block.pop()
    for width in range(1, min(3, len(block)) + 1):
        match = _CODEX_COMPACT.fullmatch(' '.join(block[-width:]))
        if match:
            return match
    return None


def _dsh_compaction(rows):
    for i in range(len(rows) - 3, max(0, len(rows) - 10), -1):
        if (re.fullmatch(r'╭─{8,}.*╮', rows[i])
                and re.match(r'(?:⌸\s+)?❯(?:\s|$)', rows[i+1])
                and re.fullmatch(r'╰─{8,}╯', rows[i+2])):
            status = rows[i-1]
            return (_DSH_COMPACT.fullmatch(status) or _DSH_COMPACT_BUSY.fullmatch(status)
                    or _DSH_COMPACT_TOTAL.fullmatch(status))
    return None


def reader_compaction(text, programs):
    """Current compact widget, matching the cockpit's reader_activity.js.

    Only recognized current composer chrome counts. Return the harness and
    the observed duration (empty for an untimed Hermes /compress notice).
    """
    if len(text) > 65536:
        return None
    lines = [row.rstrip() for row in normalize(text).split('\n')[-40:]]
    for program in programs:
        if program == 'claude':
            match = _claude_compaction(lines)
        elif program == 'codex':
            match = _codex_compaction(lines)
        elif program == 'dsh':
            match = _dsh_compaction([row.strip() for row in lines if row.strip()])
        elif program == 'hermes':
            rows = [row.strip() for row in lines if row.strip()]
            for i in range(len(rows) - 1, -1, -1):
                if re.match(r'(?:\S+\s+)?❯(?:\s|$)', rows[i]):
                    break
            else:
                continue
            for status in (*rows[max(0, i-1):i], *rows[i+1:i+2]):
                match = _HERMES_COMPACT.fullmatch(status)
                if match and _COMPACT_DURATION.fullmatch(match[1]):
                    return program, match[1]
                if _HERMES_COMPRESS.fullmatch(status):
                    return program, ''
            continue
        else:
            continue
        if match:
            elapsed = ' '.join(match[1].split())
            if program == 'dsh':
                elapsed = re.sub(r'([hm])(?=[0-9])', r'\1 ', elapsed)
            if _COMPACT_DURATION.fullmatch(elapsed):
                return program, elapsed
    return None


def context_used(text):
    for pattern, left in ((_CTX_RE,False),(_CTX_CODEX_LEFT_RE,True),(_CTX_CODEX_USED_RE,False)):
        match=pattern.search(text)
        if match:
            value=int(match.group(1))
            return (100-value if left else value) if 0<=value<=100 else None
    return None

def current_modal_activity(current):
    """Classify a modal that ends the current screen, excluding any composer."""
    # Claude wraps the permission modal before tmux captures it. Its heading
    # can be above the 16-line window, while the menu must end the capture.
    permission = " ".join(" ".join(current[-32:]).split()).replace("❯ ", "")
    # The menu lists only the permissions still missing, so after one is
    # granted it shrinks to that setting and "Try again".
    if ("Computer use needs macOS permissions " in permission
            and " Open System Settings → " in permission
            and permission.endswith(" Try again Enter to confirm · Esc to cancel")):
        return "ask"

    # These footer controls must still be current. Looking at the last control
    # rather than reversing work/ask priority also preserves new work after an
    # answered modal that remains in the scrollback.
    rows = [row.strip() for row in current[-16:] if row.strip()]
    if not rows:
        return None
    # Antigravity puts the approval controls above its model status bar.
    if (len(rows) >= 5 and rows[-2] == _AGY_COMMAND_APPROVAL_CONTROLS
            and _AGY_APPROVAL_FOOTER_RE.fullmatch(rows[-1])):
        for index, row in enumerate(rows[:-4]):
            if row != "Run this command?":
                continue
            options = _AGY_COMMAND_OPTION_BREAK_RE.split("\n".join(rows[index + 1:-2]))
            # The persistent allowances wrap at narrower terminal widths.
            choices = [" ".join(option.removeprefix(">").split()) for option in options]
            if (len(choices) >= 2 and choices[0] == "1. Yes, run command"
                    and re.fullmatch(r"[1-9]\d*\. No, cancel", choices[-1])
                    and any(option.startswith("> ") for option in options)
                    and all(_AGY_COMMAND_ALWAYS_RE.fullmatch(choice) for choice in choices[1:-1])):
                return "ask"
    # Codex hook trust has its own footer, not the normal command-approval one.
    # Require the complete current menu transcribed from the reviewed screen;
    # the title or a prose explanation of hook trust is not an approval UI.
    hook = rows[-7:]
    if (len(hook) == 7 and hook[0] == "Hooks need review"
            and re.fullmatch(r"[1-9]\d* hooks? (?:is|are) new or changed\.", hook[1])
            and hook[2] == "Hooks can run outside the sandbox after you trust them."
            and [re.sub(r"^[›❯>]\s*", "", row) for row in hook[3:6]] == [
                "1. Review hooks", "2. Trust all and continue",
                "3. Continue without trusting (hooks won't run)"]
            and any(re.match(r"[›❯>]\s*[123]\.", row) for row in hook[3:6])
            and hook[6] == "enter confirm · esc skip"):
        return "ask"
    # The existing question literal may put its footer before numbered choices.
    # Only those choices may follow it; arbitrary later output is not current UI.
    for index, row in enumerate(rows):
        if (_QUESTION_RE.fullmatch(row)
                and all(re.fullmatch(r"[❯>›]?\s*\d+\.\s+.+", option)
                        for option in rows[index + 1:])):
            return "question"
    if _ASK_CODEX_RE.fullmatch(rows[-1]) or _ASK_YN_RE.search(rows[-1]):
        return "ask"
    for index, row in enumerate(rows):
        if _ASK_Q_RE.match(row) and rows[index + 1:]:
            options = rows[index + 1:]
            # Claude's edit approval places this control hint after the menu.
            # Keep the question and numbered choices mandatory; arbitrary later
            # output must still invalidate an old approval screen.
            if options[-1] in {"Esc to cancel", "Esc to cancel · Tab to amend"}:
                options = options[:-1]
                if options and _CLAUDE_APPROVAL_SCROLL_RE.fullmatch(options[-1]):
                    options = options[:-1]
            if (any(_ASK_OPT_RE.match(option) for option in options)
                    and all(re.fullmatch(r"[❯>›]?\s*\d+\.\s+.+", option) for option in options)):
                return "ask"
    return None


def activity(text):
    # Compact admission refuses even incomplete recent controls. Keep that
    # conservative evidence separate from asserting a current displayed modal.
    if _WORK_RE.search(text) or _WORK_CODEX_RE.search(text) or _WORK_CLAUDE_TASK_RE.search('\n'.join(text.splitlines()[-25:])):
        return 'work'
    lines = text.splitlines()
    if modal := current_modal_activity(lines):
        return modal
    if _QUESTION_RE.search('\n'.join(lines[-12:])):
        return 'question'
    tail = '\n'.join(lines[-16:])
    if _ASK_CODEX_RE.search(tail) or (_ASK_Q_RE.search(tail) and _ASK_OPT_RE.search(tail)) or _ASK_YN_RE.search(tail):
        return 'ask'
    return 'wait'


def prompt_suffix(text,provider):
    glyph={'claude':'❯','codex':'›'}.get(provider)
    if glyph is None:return None
    rows=[line.strip()[1:].strip() for line in normalize(text).splitlines() if line.strip().startswith(glyph)]
    return rows[-1] if rows else None

def codex_footer_ready(rows):
    # Validate the entire nonblank tail, not independently skippable lines:
    # at most one model row, then at most one controls/context row.
    # Sparse legacy captures may omit either row. Repeated or reversed rows
    # can be multiline input resembling metadata and must remain ambiguous.
    tail=[line.strip() for line in rows if line.strip()]
    if tail and _CODEX_MODEL_FOOTER_RE.fullmatch(tail[0]):tail=tail[1:]
    if not tail:return True
    if len(tail)!=1:return False
    value=tail[0]
    return bool(_CODEX_SHORTCUT_FOOTER_RE.fullmatch(value)
                or _CODEX_WARNING_FOOTER_RE.fullmatch(value)
                or _CTX_RE.fullmatch(value) or _CTX_CODEX_LEFT_RE.fullmatch(value)
                or _CTX_CODEX_USED_RE.fullmatch(value))


def codex_model_effort(text):
    """Read the current model and effort below a recognizable Codex composer."""
    rows = normalize(text).splitlines()
    first = max(0, len(rows) - 12)
    for index in range(len(rows) - 1, first - 1, -1):
        prompt = re.fullmatch(r"›(?:\s+(.*))?", rows[index].rstrip())
        if not prompt:
            continue
        tail = [row for row in rows[index + 1:] if row.strip()]
        if not tail or len(tail) > 2:
            break
        if not tail[0].startswith("  "):
            break
        visible_controls = False
        if len(tail) == 2:
            controls = tail[1].strip().removeprefix("← for agents · ")
            visible_controls = bool(_CODEX_SHORTCUT_FOOTER_RE.fullmatch(controls)
                                    or _CODEX_WARNING_FOOTER_RE.fullmatch(controls))
            if not codex_footer_ready([controls]) or _CODEX_MODEL_FOOTER_RE.fullmatch(controls):
                break
        if prompt[1] not in (None, "Ask Codex to do anything") and not visible_controls:
            break
        match = _CODEX_CARD_MODEL_RE.fullmatch(tail[0].strip())
        if match:
            return match["model"], match["effort"].lower()
        if visible_controls:
            match = _CODEX_CARD_MODEL_ONLY_RE.fullmatch(tail[0].strip())
            if match:
                return match["model"], None
        break
    return None, None

def compact_readiness(text,provider):
    text=normalize(text)
    if provider not in ('claude','codex'):
        return dict(state='unknown',input_empty=None,context_used_percent=context_used(text))
    suffix=prompt_suffix(text,provider)
    # Only this observed native placeholder is known to mean no input.
    # Unknown prompts and Claude's unstyled suggestions remain pending input.
    if provider=='codex' and suffix=='Ask Codex to do anything':suffix=''
    rows=text.splitlines()
    glyph={'claude':'❯','codex':'›'}[provider]
    indices=[i for i,line in enumerate(rows) if line.strip().startswith(glyph)]
    ambiguous=False
    if suffix=='' and indices:
        tail=rows[indices[-1]+1:]
        if provider=='codex':ambiguous=not codex_footer_ready(tail)
        else:
            for line in tail:
                value=line.strip()
                if not value:continue
                if re.fullmatch(r'[─━]+.*',value):break
                if (_CTX_RE.search(value) or _CTX_CODEX_LEFT_RE.search(value) or _CTX_CODEX_USED_RE.search(value)
                        or re.match(r'gpt-\S+\s+(?:low|medium|high|xhigh|max|ultra)\b',value)
                        or value.startswith(('? for shortcuts','⏵','←','→','% context left'))):continue
                ambiguous=True;break
    state=activity(text)
    if 'Selected model is at capacity. Please try a different model.' in text or state in ('ask','question') or suffix is None or ambiguous:
        state='unknown'
    elif state=='wait':state='idle'
    return dict(state=state,input_empty=None if suffix is None or ambiguous else not bool(suffix),context_used_percent=context_used(text))

def compact_refusal(readiness):
    if readiness['state']!='idle' or readiness['input_empty'] is None:return 'TMUX_TARGET_NOT_IDLE'
    if not readiness['input_empty']:return 'TMUX_TARGET_INPUT_NOT_EMPTY'
    return None


def spawn_prompt_ready(text):
    """Released Claude startup policy; stricter compact policy is separate."""
    tail='\n'.join([line for line in normalize(text).splitlines() if line.strip()][-8:])
    if 'for shortcuts' in tail:return True
    suffix=prompt_suffix(tail,'claude')
    return suffix is not None and (not suffix or suffix.startswith('Try "'))


if __name__=='__main__':
    import sys
    if sys.argv[1:]==['spawn-ready']:
        raise SystemExit(0 if spawn_prompt_ready(sys.stdin.read()) else 1)
    raise SystemExit(2)
