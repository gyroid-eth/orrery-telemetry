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
  const EXIT_SHIFT_GUARD_MS=2000,EXIT_SPOT_SLACK=8;let exitShiftUntil=0,exitShiftTimer=0;const exitWatch=new Map();
  const classes=new Set(),button={textContent:'↩ EXIT'};
  // Where the card stands: its section (cat-*) and place; gone when null.
  const spot={cat:'cat-agent',top:100,gone:false};
  const card={classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c)},
              querySelector:()=>button,get className(){return 'bay '+spot.cat;},
              getBoundingClientRect:()=>({left:20,top:spot.top})};
  const document={querySelector:()=>spot.gone?null:card},CSS={escape:x=>x},scrollX=0,scrollY=0;
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
                      ('notifyExitSent', 'exitFailureText', 'canKill', 'showExitSent', 'holdExitButtons', 'exitCardSpot',
                       'exitCardMoved', 'watchExitCards', 'settleExitSent', 'exitAgent'))
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
    render = function('renderDeck')
    assert 'for(const nm of exitSentAt.keys())' in render and 'showExitSent(' in render
    # Every redraw compares the confirmed EXITs' cards, whatever asked for it.
    assert 'function render(){renderDeck();watchExitCards();}' in SOURCE
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


def _guard(js, response, hold=False):
    """Confirm Pilot's EXIT, then run js; report whether Other could be armed.
    With hold, /api/exit does not answer until after js and the checks."""
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    status, payload = response
    script = PRELUDE + (
        "let answer;const replied=new Promise(r=>{answer=r;});"
        "async function fetch(url,init){requests.push(JSON.parse(init.body));"
        + ("await replied;" if hold else "") +
        f"return {{ok:{str(status == 200).lower()},status:{status},json:async()=>({json.dumps(payload)})}};}}"
    )
    script += "".join(function(name) for name in
                      ('notifyExitSent', 'exitFailureText', 'canKill', 'showExitSent', 'holdExitButtons', 'exitCardSpot',
                       'exitCardMoved', 'watchExitCards', 'settleExitSent', 'exitAgent'))
    script += """(async()=>{await press();const sent=press();
      await new Promise(r=>setImmediate(r));
      const pendingDuring=requests.length===1&&exitSentAt.size===0;
      """ + js + """
      exitingSet.clear();exitAgent({stopPropagation(){}},'Other');
      const otherArmed=exitingSet.has('Other');
      exitShiftUntil=0;exitingSet.clear();exitAgent({stopPropagation(){}},'Other');
      const armedAfter=exitingSet.has('Other');
      answer();await sent;
      console.log(JSON.stringify({pendingDuring,otherArmed,armedAfter,requests:requests.length}));
    })();"""
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_pause_starts_with_the_confirming_press_whatever_the_reply():
    """Full tour recording: right after the child's EXIT was confirmed, the
    parent's EXIT slid under the pointer and one more click armed it."""
    for response in [(200, {'ok': True, 'actions': ['exit-sent']}), (400, {'ok': False, 'error': 'boom'})]:
        result = _guard("", response)
        assert result['otherArmed'] is False and result['armedAfter'] is True, (response, result)
        assert result['requests'] == 1


@pytest.mark.parametrize('move,paused', [('spot.gone=true;', True), ("spot.cat='cat-finished';spot.top=600;", True),
                                         ('spot.top=400;', True), ('spot.top=102;', False)],
                         ids=['card-leaves', 'card-moves-to-finished', 'card-moves', 'own-state-nudge'])
def test_the_pause_starts_again_when_the_card_moves_even_before_the_reply(move, paused):
    """Review of #207: the card can leave LIVE or move to finished before
    /api/exit answers, or long after; each is when another card takes its
    place. Here the pause from the press has run out, the reply is still
    pending, and the card moves."""
    result = _guard("exitShiftUntil=0;" + move + "watchExitCards();", (200, {'ok': True, 'actions': ['exit-sent']}), hold=True)
    assert result['pendingDuring'] is True, result
    # A nudge of a pixel or two from the card's own state is not a move.
    assert result['otherArmed'] is (not paused) and result['armedAfter'] is True, result
