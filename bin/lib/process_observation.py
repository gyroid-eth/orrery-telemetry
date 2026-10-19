"""Shared lightweight process evidence; no global client/config initialization."""
import subprocess
import time
import signal
import os
from datetime import datetime, timezone


def request_provider_exit(*, send, interrupt, delegated_antigravity=False,
                          shell_exit=False, pause=time.sleep):
    """The existing dashboard exit policy, with proven effects injected.

    Callers own admission and liveness. Submission never proves termination.
    A delegated stream runner must survive to publish its provider's outcome.
    """
    if delegated_antigravity:
        interrupt(signal.SIGINT)
        return 'interrupt-sent'
    if shell_exit:
        send('exit', 'Enter')
        return 'shell-exit-sent'
    send('-l', '/exit')
    pause(0.3)
    send('C-m')
    return 'exit-sent'


class ClientError(RuntimeError):
    pass



def integer(value, minimum=0):
    return type(value) is int and value >= minimum



def remaining(deadline, limit=5):
    if deadline is None:
        return limit
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise ClientError('OBSERVATION_INCOMPLETE')
    return min(limit, budget)



def process_start(pid, *, deadline=None, clock=None):
    if not integer(pid,1) or clock not in (None,'UTC'):
        return None
    try:
        options = {'env':{**os.environ,'TZ':'UTC0','LC_ALL':'C'}} if clock=='UTC' else {}
        result = subprocess.run(['ps','-p',str(pid),'-o','lstart='], capture_output=True, text=True,
                                timeout=remaining(deadline, 3), **options)
        if result.returncode == 1 and not result.stderr:
            return 'absent'
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def ancestry(provider, runner, uid, *, deadline=None):
    """Ephemeral exact same-UID chain; None means unknown or not related."""
    if not integer(provider,1) or not integer(runner,1) or not integer(uid):return None
    try:
        result=subprocess.run(['ps','-axo','pid=,ppid=,uid='],capture_output=True,text=True,timeout=remaining(deadline))
        if result.returncode:return None
        rows={}
        for line in result.stdout.splitlines():
            parts=line.split()
            if len(parts)>=3 and all(x.isdigit() for x in parts[:3]):
                pid,parent,owner=map(int,parts[:3]);rows[pid]=(parent,owner)
        seen=set();current=provider;chain=[]
        while current!=runner:
            if current in seen or current not in rows or rows[current][1]!=uid:return None
            chain.append((current,*rows[current]))
            seen.add(current);current=rows[current][0]
        if runner not in rows or rows[runner][1]!=uid:return None
        return tuple(chain+[(runner,*rows[runner])])
    except (OSError,ValueError,subprocess.SubprocessError):return None


def belongs_to(provider, runner, uid, *, deadline=None):
    """Preserve the boolean API; do not persist the observed parent chain."""
    return ancestry(provider,runner,uid,deadline=deadline) is not None


def _utc_birth(value):
    try:
        if (not isinstance(value,dict) or set(value)!={'clock','lstart'}
                or value['clock']!='UTC' or not isinstance(value['lstart'],str)):
            return None
        parsed=datetime.strptime(value['lstart'],'%a %b %d %H:%M:%S %Y')
        if value['lstart'].split()[0]!=('Mon','Tue','Wed','Thu','Fri','Sat','Sun')[parsed.weekday()]:
            return None
        return parsed.replace(tzinfo=timezone.utc)
    except (KeyError,TypeError,ValueError,OverflowError):return None


def birth_not_before(parent, child):
    """Known UTC observations only; second-resolution equality is valid."""
    parent,child=_utc_birth(parent),_utc_birth(child)
    return parent is not None and child is not None and child>=parent


def process_birth_utc(pid, *, deadline=None):
    """Fresh known-zone observation; never relabel a stored local lstart."""
    value={'clock':'UTC','lstart':process_start(pid,deadline=deadline,clock='UTC')}
    return value if _utc_birth(value) is not None else None


def auth_process_binding(helper, provider, runner, uid, *, deadline=None):
    """Prove one live R2 ancestry twice without a second PID authority.

    Saved runner birth retains its original local representation. Chronology
    uses only fresh UTC observations for ALL three processes. Exact ephemeral
    parent chains and both birth representations must remain unchanged.
    """
    if (not isinstance(runner,dict) or set(runner)!={'pid','process_start'}
            or not integer(uid) or not all(integer(pid,1) for pid in (helper,provider,runner['pid']))
            or len({helper,provider,runner['pid']})!=3
            or not isinstance(runner['process_start'],str) or runner['process_start'] in ('','absent')):
        return False
    if deadline is None:deadline=time.monotonic()+5
    def snapshot():
        pids=(runner['pid'],provider,helper)
        original={pid:process_start(pid,deadline=deadline) for pid in pids}
        if (original[runner['pid']]!=runner['process_start']
                or any(not isinstance(value,str) or value in ('','absent') for value in original.values())):
            return None
        utc={pid:process_birth_utc(pid,deadline=deadline) for pid in pids}
        if (not birth_not_before(utc[runner['pid']],utc[provider])
                or not birth_not_before(utc[provider],utc[helper])):return None
        child_chain=ancestry(helper,provider,uid,deadline=deadline)
        provider_chain=ancestry(provider,runner['pid'],uid,deadline=deadline)
        if child_chain is None or provider_chain is None:return None
        return original,utc,child_chain,provider_chain
    try:
        before=snapshot()
        return before is not None and before==snapshot()
    except (OSError,TypeError,ValueError,ClientError,subprocess.SubprocessError):return False


def active_provider_binding(helper,provider,runner,uid,*,deadline=None):
    """The same ancestry proof plus the provider's saved birth before/after.

    Shared by legacy auth and its proxy. Unknown observations never count as
    matching; UTC chronology remains in auth_process_binding, not a new parser.
    """
    if (not isinstance(provider,dict) or set(provider)!={'pid','process_start'}
            or not integer(provider['pid'],1) or not isinstance(provider['process_start'],str)
            or provider['process_start'] in ('','absent')):return False
    if deadline is None:deadline=time.monotonic()+5
    try:
        pid=provider['pid'];expected=provider['process_start']
        return (process_start(pid,deadline=deadline)==expected
            and auth_process_binding(helper,pid,runner,uid,deadline=deadline)
            and process_start(pid,deadline=deadline)==expected)
    except (OSError,TypeError,ValueError,ClientError,subprocess.SubprocessError):return False
