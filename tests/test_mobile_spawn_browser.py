"""Mobile launch form and failure copy against a private browser fixture."""
import json
import pytest

from dashboard.mobile_spawn import MobileSpawn
from mobile_reader_browser import phone  # noqa: F401
from test_mobile_spawn import local_catalog


def open_local_editor(phone, source=local_catalog):
    _, js, wait, state = phone
    state['spawn_catalog'] = MobileSpawn(source, None, None).catalog('fixture', configurable=True)
    js("OrreryMobileTerminal.close();document.querySelector('#mobile-new-agent').click()")
    wait("!document.querySelector('#spawn-editor').hidden")
    js("""document.querySelector('#spawn-model-chip').click();
        document.querySelector('#spawn-provider-tabs [aria-label=Local]').click();
        document.querySelector('#spawn-settings-close').click();
        const task=document.querySelector('#spawn-task');
        task.value='Review the cache benchmark and summarize the result.';
        task.dispatchEvent(new Event('input'));""")


@pytest.mark.parametrize('reason, message', [
    ('unsupported_tools', 'Turn off optional tools or choose another harness.'),
    ('model_unavailable', 'Choose another model or harness.'),
    ('/fixture/private configuration', 'Check the launch settings and try again.'),
])
def test_local_tool_selection_and_launch_failure(phone, reason, message):
    _, js, wait, state = phone
    open_local_editor(phone)
    assert js("document.querySelector('#spawn-model-name').textContent") == 'inkling-2-mini (URL 1)'
    assert js("document.querySelector('#spawn-harness').value") == 'hermes'
    assert js("document.querySelector('#spawn-computer').getAttribute('aria-checked')") == 'true'
    assert js("document.querySelector('#spawn-browser').closest('.spawn-tool').hidden")
    js("const directory=document.querySelector('#spawn-directory');directory.value='1';directory.dispatchEvent(new Event('change'))")
    assert not js("document.querySelector('#spawn-browser').closest('.spawn-tool').hidden")
    assert js("document.querySelector('#spawn-browser').getAttribute('aria-checked')") == 'true'
    js("directory.value='0';directory.dispatchEvent(new Event('change'));document.querySelector('#spawn-editor').requestSubmit()")
    assert js("document.querySelector('#spawn-browser').getAttribute('aria-checked')") == 'false'
    assert not js("Boolean(document.querySelector('#spawn-summary [aria-label^=Browser]'))")
    state['spawn_receipt'] = {'state':'failed','failure_reason':reason}
    js("document.querySelector('#spawn-confirm').click()")
    wait("document.querySelector('#spawn-notice').textContent.startsWith('Launch failed.')")
    assert js("document.querySelector('#spawn-notice').innerText") == 'Launch failed.\n' + message
    assert js("document.querySelector('#spawn-result button').textContent") == 'Edit'
    posted = [body for operation, body in state['calls'] if operation == 'spawn']
    assert len(posted) == 1
    assert posted[0]['computer'] and 'browser' not in posted[0]
    js("document.querySelector('#spawn-result button').click()")
    wait("!document.querySelector('#spawn-editor').hidden")
    assert js("document.querySelector('#spawn-task').value") == posted[0]['task']


def test_browser_icon_opens_three_modes_and_posts_the_choice(phone):
    _, js, wait, state = phone
    open_local_editor(phone, lambda: {**local_catalog(), 'browser_modes':{'hermes':['off','temporary','personal']}})
    assert js("document.querySelector('#spawn-browser').getAttribute('aria-label')") == 'Browser: My Browser'
    js("document.querySelector('#spawn-browser').click()")
    wait("document.querySelector('#spawn-browser-menu').matches(':popover-open')")
    assert js("[...document.querySelectorAll('#spawn-browser-menu button:not([hidden])')].map(b=>b.textContent)") == [
        'Off','Temporary Chrome','My Browser']
    js("document.querySelectorAll('#spawn-browser-menu button')[1].click()")
    assert js("document.querySelector('#spawn-browser').dataset.browserMode") == 'temporary'
    js("document.querySelector('#spawn-editor').requestSubmit()")
    assert js("document.querySelector('#spawn-summary').textContent.includes('Temporary Chrome')")
    state['spawn_receipt'] = {'state':'ready','name':'FixtureCurie'}
    js("document.querySelector('#spawn-confirm').click()")
    wait("document.querySelector('#spawn-notice').textContent.includes('is ready')")
    posted = [body for operation, body in state['calls'] if operation == 'spawn']
    assert posted[0]['browser_mode'] == 'temporary' and 'browser' not in posted[0]


@pytest.mark.parametrize('ready', [True, False])
def test_mobile_my_browser_names_the_chosen_brave_profile_or_setup(phone, ready):
    _, js, wait, state = phone
    reason = '' if ready else 'Open brave://inspect/#remote-debugging in Brave and enable remote debugging.'
    open_local_editor(phone, lambda: {**local_catalog(),
        'browser_modes': {'hermes': ['off', 'temporary', 'personal']},
        'personal_browser_status': {'hermes': {'browser': 'Brave', 'reason': reason, 'unavailable': not ready}},
    })
    assert js("document.querySelector('#spawn-notice').textContent") == 'My Browser: ' + (reason or 'Brave')
    assert js("document.querySelector('#spawn-browser').getAttribute('aria-label')") == (
        'Browser: My Browser (Brave)' if ready else 'Browser: Temporary Chrome'
    )
    js("document.querySelector('#spawn-browser').click()")
    wait("document.querySelector('#spawn-browser-menu').matches(':popover-open')")
    assert js("document.querySelectorAll('#spawn-browser-menu button')[2].disabled") is not ready
    js("document.querySelector('#spawn-browser-menu').hidePopover();document.querySelector('#spawn-editor').requestSubmit()")
    assert js("document.querySelector('#spawn-summary [aria-label^=Browser]').getAttribute('aria-label')") == (
        'Browser: My Browser (Brave)' if ready else 'Browser: Temporary Chrome'
    )
    state['spawn_receipt'] = {'state':'ready','name':'FixtureCurie'}
    js("document.querySelector('#spawn-confirm').click()")
    wait("document.querySelector('#spawn-notice').textContent.includes('is ready')")
    [posted] = [body for operation, body in state['calls'] if operation == 'spawn']
    assert posted['browser_mode'] == ('personal' if ready else 'temporary')


def test_switching_to_antigravity_hides_tools_and_clears_the_launch_selection(phone):
    _, js, wait, state = phone
    catalog = local_catalog()
    catalog['browser_modes'] = {'hermes':['off','temporary','personal']}
    catalog['providers'].append({
        'id':'gemini', 'label':'Antigravity', 'efforts':['low','high'],
        'capabilities':{'standalone':True, 'effort_required':True},
        'models':[{'id':'gemini-fixture'}],
        'harnesses':[{'id':'antigravity', 'available':True, 'default':True}],
    })
    open_local_editor(phone, lambda: catalog)
    js("""document.querySelector('#spawn-browser').click();
        document.querySelectorAll('#spawn-browser-menu button')[1].click();
        document.querySelector('#spawn-model-chip').click();
        document.querySelector('#spawn-provider-tabs [aria-label=Antigravity]').click();""")
    assert js("document.querySelector('#spawn-tools').hidden")
    assert not js("document.querySelector('#spawn-browser-menu').matches(':popover-open')")
    js("""const effort=document.querySelector('#spawn-effort');
        effort.value='low';effort.dispatchEvent(new Event('change'));
        document.querySelector('#spawn-settings-close').click();
        document.querySelector('#spawn-editor').requestSubmit();""")
    assert not js("Boolean(document.querySelector('#spawn-summary [aria-label^=Browser],#spawn-summary [aria-label^=Computer]'))")
    state['spawn_receipt'] = {'state':'ready','name':'FixtureCurie'}
    js("document.querySelector('#spawn-confirm').click()")
    wait("document.querySelector('#spawn-notice').textContent.includes('is ready')")
    [posted] = [body for operation, body in state['calls'] if operation == 'spawn']
    assert posted['harness'] == 'antigravity' and posted['computer'] is False
    assert 'browser' not in posted and 'browser_mode' not in posted


@pytest.mark.parametrize('provider,harness', [
    ('claude','claude-code'), ('codex','codex-cli'), ('deepseek','dsh'),
    ('deepseek','hermes'), ('gemini','antigravity'),
])
def test_five_harnesses_offer_the_three_computer_use_modes(phone, provider, harness):
    _, js, wait, state = phone
    source = {
        'dirs':[{'path':'/workspace','label':'Workspace'}],
        'computer_modes':['off','view','control'],
        'browser_modes':{'codex' if harness=='codex-cli' else harness:['off','temporary','personal']},
        'providers':[{'id':provider,'label':provider,'capabilities':{'standalone':True},
                      'models':[{'id':'fixture-model','default':True}],
                      'harnesses':[{'id':harness,'available':True,'default':True}]}],
    }
    state['spawn_catalog'] = MobileSpawn(lambda:source, None, None).catalog('fixture', configurable=True)
    js("OrreryMobileTerminal.close();document.querySelector('#mobile-new-agent').click()")
    wait("!document.querySelector('#spawn-editor').hidden")
    assert js("document.querySelector('#spawn-harness').value") == harness
    for mode, label in [('off','Off'), ('control','Control'), ('view','View')]:
        js("document.querySelector('#spawn-computer').click()")
        wait("document.querySelector('#spawn-computer-menu').matches(':popover-open')")
        assert js("[...document.querySelectorAll('#spawn-computer-menu button:not([hidden])')].map(b=>b.textContent)") == [
            'Off','View','Control']
        js("([...document.querySelectorAll('#spawn-computer-menu button')].find(b=>b.textContent==="+json.dumps(label)+")).click()")
        assert js("document.querySelector('#spawn-computer').dataset.computerMode") == mode
    js("""document.querySelector('#spawn-browser').click();
        document.querySelectorAll('#spawn-browser-menu button')[1].click();
        const task=document.querySelector('#spawn-task');task.value='Review the workspace.';
        task.dispatchEvent(new Event('input'));document.querySelector('#spawn-editor').requestSubmit();""")
    assert js("document.querySelector('#spawn-summary').textContent.includes('View')")
    state['spawn_receipt'] = {'state':'ready','name':'FixtureCurie'}
    js("document.querySelector('#spawn-confirm').click()")
    wait("document.querySelector('#spawn-notice').textContent.includes('is ready')")
    [posted] = [body for operation, body in state['calls'] if operation == 'spawn']
    assert posted['computer'] == 'view' and posted['browser_mode'] == 'temporary'
