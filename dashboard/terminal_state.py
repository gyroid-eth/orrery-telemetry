"""Pure terminal evidence shared by observation and explicit compact admission."""
import re

_CTX_RE = re.compile(r"ctx:\s*(\d+)%\s*used")
_CTX_CODEX_LEFT_RE = re.compile(r"Context\s+(\d+)%\s*left", re.IGNORECASE)
_CTX_CODEX_USED_RE = re.compile(r"Context\s+(\d+)%\s*used", re.IGNORECASE)
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

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_STALL_WORK = re.compile(r"(?im)^\s*[•▌●]\s*(?:Working|Thinking)\b[^\n]*\([^\n]*(?:\d+\s*s|esc to interrupt)")

def normalize(text):
    return _ANSI.sub('', text).replace('\r','').replace('\xa0',' ')

def context_used(text):
    for pattern, left in ((_CTX_RE,False),(_CTX_CODEX_LEFT_RE,True),(_CTX_CODEX_USED_RE,False)):
        match=pattern.search(text)
        if match:
            value=int(match.group(1))
            return (100-value if left else value) if 0<=value<=100 else None
    return None

def activity(text):
    # Preserve the existing dashboard windows and priority exactly.
    if _WORK_RE.search(text) or _WORK_CODEX_RE.search(text) or _WORK_CLAUDE_TASK_RE.search('\n'.join(text.splitlines()[-25:])):
        return 'work'
    if _QUESTION_RE.search('\n'.join(text.splitlines()[-12:])):return 'question'
    tail='\n'.join(text.splitlines()[-16:])
    if _ASK_CODEX_RE.search(tail) or (_ASK_Q_RE.search(tail) and _ASK_OPT_RE.search(tail)) or _ASK_YN_RE.search(tail):return 'ask'
    return 'wait'

def prompt_suffix(text,provider):
    glyph={'claude':'❯','codex':'›'}.get(provider)
    if glyph is None:return None
    rows=[line.strip()[1:].strip() for line in normalize(text).splitlines() if line.strip().startswith(glyph)]
    return rows[-1] if rows else None

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
        for line in rows[indices[-1]+1:]:
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
