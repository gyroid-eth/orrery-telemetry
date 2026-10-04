"""Deck EXIT: a delivered EXIT keeps its card "exiting" until the agent is gone.

The card used to return to "↩ EXIT" right after /api/exit answered, while the
tmux session still took a few seconds to end; a second press then reached
/api/exit again and got a 400 (Windows trial, 2026-10-04).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = (Path(__file__).resolve().parents[1] / 'dashboard/index.html').read_text()


def function(name):
    return re.search(r'(?:async )?function ' + name + r'\(.*?\n}', SOURCE, re.S).group()


PRELUDE = """
  const EMBED_MODE=false,toasts=[],requests=[];
  const toast=(title,text,bad)=>toasts.push({title,text,bad:!!bad});
  const notifyTourAction=()=>{};
  const SHOW_DEFAULT=new Set(['agent','finished','unnamed']);
  const exitingSet=new Set(),exitTimers=new Map(),exitSentAt=new Map(),ARM_MS=5000,EXIT_SETTLE_MS=30000;
  const EXIT_SHIFT_GUARD_MS=2000;let exitShiftUntil=0;
  const classes=new Set(),button={textContent:'↩ EXIT'};
  const card={classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c)},
              querySelector:()=>button};
  const document={querySelector:()=>card},CSS={escape:x=>x};
  const setTimeout=()=>1,clearTimeout=()=>{};
  const press=()=>exitAgent({stopPropagation(){}},'Pilot');
"""


def run(js, response):
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    status, payload = response
    script = PRELUDE + (
        f"async function fetch(url,init){{requests.push(JSON.parse(init.body));"
        f"return {{ok:{str(status == 200).lower()},status:{status},json:async()=>({json.dumps(payload)})}};}}"
    )
    script += "".join(function(name) for name in
                      ('notifyExitSent', 'exitFailureText', 'canKill', 'showExitSent', 'settleExitSent', 'exitAgent'))
    script += js
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


SNAPSHOT = """console.log(JSON.stringify({requests:requests.length,classes:[...classes].sort(),
  button:button.textContent,sent:[...exitSentAt.keys()],toasts}));"""


def test_a_delivered_exit_keeps_the_card_exiting_and_a_second_press_sends_nothing():
    result = run("(async()=>{await press();await press();await press();await press();" + SNAPSHOT + "})();",
                 (200, {'ok': True, 'actions': ['exit-sent']}))
    assert result['requests'] == 1
    assert result['classes'] == ['exiting']
    assert result['button'] == 'exiting…'
    assert result['sent'] == ['Pilot']


def test_a_failed_exit_gives_the_button_back_and_says_why():
    result = run("(async()=>{await press();await press();" + SNAPSHOT + "})();",
                 (400, {'ok': False, 'error': 'boom'}))
    assert result['requests'] == 1
    assert result['classes'] == []
    assert result['button'] == '↩ EXIT'
    assert result['sent'] == []
    assert result['toasts'][-1] == {'title': '✕ FAIL', 'text': '> boom', 'bad': True}


@pytest.mark.parametrize('error', [
    "agent 'Pilot' not found",
    "agent 'Pilot' category=gone - only running/finished are exitable",
    "tmux session 'Pilot' not found",
])
def test_an_agent_that_has_already_ended_is_reported_in_words(error):
    result = run("(async()=>{await press();await press();" + SNAPSHOT + "})();",
                 (400, {'ok': False, 'error': error}))
    assert result['toasts'][-1]['text'] == '> Pilot has already exited'


RUNNING = {'name': 'Pilot', 'category': 'agent', 'running': True}
STOPPED = {'name': 'Pilot', 'category': 'finished', 'running': False, 'attached': False}
WATCHED = {'name': 'Pilot', 'category': 'finished', 'running': False, 'attached': True}


@pytest.mark.parametrize('agents,elapsed,kept,note', [
    ([RUNNING], 5000, True, None),                                      # still ending
    ([{'name': 'Pilot', 'category': 'gone'}], 5000, False, None),       # left LIVE
    ([], 5000, False, None),                                            # not listed
    ([RUNNING], 30000, False, 'Pilot is still running 30s after /exit; EXIT is available again'),
    # Review of #197: a stopped agent's card and panel have no EXIT; its card
    # has KILL only when no one is attached, so KILL is named only then.
    ([STOPPED], 30000, False, 'Pilot has stopped but its session is still open 30s after /exit; '
                              'KILL on its card can end it'),
    ([WATCHED], 30000, False, 'Pilot has stopped but its session is still open 30s after /exit'),
])
def test_the_exiting_state_ends_when_the_agent_leaves_live_or_after_30s(agents, elapsed, kept, note):
    js = (f"exitSentAt.set('Pilot',1000);settleExitSent({json.dumps(agents)},{1000 + elapsed});"
          "console.log(JSON.stringify({kept:exitSentAt.has('Pilot'),toasts}));")
    result = run(js, (200, {'ok': True}))
    assert result['kept'] is kept
    assert [t['text'] for t in result['toasts']] == ([] if note is None else ['> ' + note])


def test_render_and_tick_keep_the_state_across_redraws():
    render = function('render')
    assert 'for(const nm of exitSentAt.keys())' in render and 'showExitSent(' in render
    tick = function('tick')
    assert tick.index('settleExitSent(j.agents)') < tick.index('render()')


def _card_buttons(agent):
    """The buttons the real bay() draws for an agent. bay() reads many display
    helpers; inside a catch-all scope every name it does not get here is a
    function returning '', so only canKill and the agent decide the buttons."""
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    script = (
        "const scope=new Proxy({},{has:(t,k)=>!(k in globalThis)&&k!=='a'&&k!=='i',"
        "get:(t,k)=>k===Symbol.unscopables?undefined:(()=>'')});"
        + function('canKill')
        + "with(scope){" + re.search(r'function bay\(a,i\)\{.*?\n}', SOURCE, re.S).group()
        + f"\nconsole.log(JSON.stringify(bay({json.dumps(agent)},0)));}}"
    )
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    html = json.loads(result.stdout)
    return {'kill': 'class="killbtn"' in html, 'exit': 'class="exitbtn"' in html}


@pytest.mark.parametrize('agent,kill_named', [(STOPPED, True), (WATCHED, False)])
def test_the_note_names_kill_only_when_the_card_shows_it(agent, kill_named):
    """Review of #197: name only an action that is on screen at that moment."""
    js = (f"exitSentAt.set('Pilot',1000);settleExitSent({json.dumps([agent])},31000);"
          "console.log(JSON.stringify(toasts));")
    note = run(js, (200, {'ok': True}))[0]['text']
    assert ('KILL' in note) is kill_named
    assert _card_buttons({**agent, 'task': '', 'live': ''}) == {'kill': kill_named, 'exit': False}


def test_the_card_rendering_used_above_shows_exit_for_a_running_agent():
    # The catch-all scope still renders real buttons: a running agent has EXIT, not KILL.
    assert _card_buttons({**RUNNING, 'attached': False, 'task': '', 'live': ''}) == {'kill': False, 'exit': True}


def test_a_press_just_after_an_exited_card_leaves_arms_nothing():
    """Full tour recording: after the child's EXIT was confirmed and its card
    left, the parent's EXIT slid under the pointer; a further click armed it."""
    result = run("""(async()=>{await press();await press();
      settleExitSent([]);
      const sentBefore=requests.length;classes.clear();button.textContent='↩ EXIT';
      exitSentAt.clear();
      await press();
      const armedDuringGuard=exitingSet.has('Pilot');
      exitShiftUntil=Date.now()-1;
      await press();
      console.log(JSON.stringify({sentBefore,armedDuringGuard,armedAfter:exitingSet.has('Pilot'),requests:requests.length}));
    })();""", (200, {'ok': True, 'actions': ['exit-sent']}))
    assert result == {'sentBefore': 1, 'armedDuringGuard': False, 'armedAfter': True, 'requests': 1}
