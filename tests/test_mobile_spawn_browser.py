"""Mobile launch form and failure copy against a private browser fixture."""
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
    assert js("document.querySelector('#spawn-browser').getAttribute('aria-label')") == 'Browser: My Chrome'
    js("document.querySelector('#spawn-browser').click()")
    wait("document.querySelector('#spawn-browser-menu').matches(':popover-open')")
    assert js("[...document.querySelectorAll('#spawn-browser-menu button:not([hidden])')].map(b=>b.textContent)") == [
        'Off','Temporary Chrome','My Chrome']
    js("document.querySelectorAll('#spawn-browser-menu button')[1].click()")
    assert js("document.querySelector('#spawn-browser').dataset.browserMode") == 'temporary'
    js("document.querySelector('#spawn-editor').requestSubmit()")
    assert js("document.querySelector('#spawn-summary').textContent.includes('Temporary Chrome')")
    state['spawn_receipt'] = {'state':'ready','name':'FixtureCurie'}
    js("document.querySelector('#spawn-confirm').click()")
    wait("document.querySelector('#spawn-notice').textContent.includes('is ready')")
    posted = [body for operation, body in state['calls'] if operation == 'spawn']
    assert posted[0]['browser_mode'] == 'temporary' and 'browser' not in posted[0]
