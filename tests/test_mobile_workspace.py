"""The responsive layout and theme use the packaged mobile page."""
import pytest

from mobile_reader_browser import phone  # noqa: F401


def tap_attachment_input(client, js, wait, input_id, frame=None):
    client.call('Page.setInterceptFileChooserDialog', enabled=True)
    client.call('Emulation.setTouchEmulationEnabled', enabled=True)
    origin = f'workspaceFrame({frame!r})' if frame else 'null'
    if frame:
        wait(f"""(() => {{
            const frame = {origin}, doc = frame.contentDocument;
            return !frame.getAnimations().some(animation => animation.playState === 'running')
                && parseFloat(frame.style.getPropertyValue('--workspace-compose-height')) ===
                   doc.querySelector('.reader-bar').offsetHeight + doc.querySelector('.reader-input-bar').offsetHeight;
        }})()""")
    point = js(f"""(() => {{
        const frame = {origin}, doc = frame?.contentDocument ?? document;
        const input = doc.getElementById({input_id!r}), bounds = input.getBoundingClientRect();
        const x = bounds.x + bounds.width / 2, y = bounds.y + bounds.height / 2;
        if (input.disabled || doc.elementFromPoint(x, y) !== input) return null;
        input.addEventListener('click', event => {{
            input.dataset.activated = String(event.isTrusted && doc.defaultView.navigator.userActivation.isActive);
        }}, {{once:true}});
        const offset = frame?.getBoundingClientRect() ?? {{x:0, y:0}};
        return {{x:x + offset.x, y:y + offset.y}};
    }})()""")
    assert point is not None
    client.call('Input.dispatchTouchEvent', type='touchStart', touchPoints=[point])
    client.call('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])
    wait(f"(({origin})?.contentDocument ?? document).getElementById({input_id!r})?.dataset.activated === 'true'")


def test_phone_activity_and_tablet_split_use_the_same_views(phone):
    client, js, wait, state = phone
    js('OrreryMobileTerminal.close()')
    wait("!document.querySelector('#terminal-sheet').open")
    first_minute = 1_790_400_000
    state['snapshot']['timeline']['mail'] = [
        dict(key=f'{index:032x}', source='SampleAgent', target='Peer', kind='to', event_at=stamp)
        for index, stamp in enumerate((first_minute + 2, first_minute + 1, first_minute - 1), 1)
    ]
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelectorAll('.activity-mail').length === 3")
    assert js("""(() => {
        const graph = document.querySelector('#net').getBoundingClientRect();
        const sidebar = document.querySelector('.mobile-sidebar').getBoundingClientRect();
        return graph.bottom <= sidebar.top && sidebar.bottom <= innerHeight &&
            document.querySelector('#activity').hidden &&
            document.querySelector('#net').contains(document.querySelector('#summary'));
    })()""")

    js("document.querySelector('button[data-view=activity]').click()")
    assert js("!document.querySelector('#activity').hidden && document.querySelector('#graph-panel').hidden")
    assert js("document.activeElement.dataset.view") == 'graph'
    js("window.timeVisibility = () => Array.from(document.querySelectorAll('.activity-mail time'), "
       "stamp => getComputedStyle(stamp).visibility)")
    assert js('timeVisibility()') == ['visible', 'visible', 'visible']
    js("""window.recipientOnSameLine = () => {
        const people = document.querySelector('.activity-mail').querySelectorAll('.person');
        const [from, to] = Array.from(people, person => person.getBoundingClientRect());
        return Math.abs(from.top + from.height / 2 - to.top - to.height / 2) < 1;
    };""")
    assert js('recipientOnSameLine()')
    assert js("""(() => {
        const row = document.querySelector('.activity-mail');
        const sender = row.querySelector('.person .name').getBoundingClientRect();
        const arrow = row.querySelector('.mail-forward').getBoundingClientRect();
        const recipient = row.querySelector('.route-to .face').getBoundingClientRect();
        return Math.abs(arrow.left + arrow.width / 2 - (sender.right + recipient.left) / 2) < 3;
    })()""")

    for width, height in [(820, 1180), (1180, 820)]:
        client.call('Emulation.setDeviceMetricsOverride', width=width, height=height,
                    deviceScaleFactor=2, mobile=True)
        wait(f"innerWidth === {width} && innerHeight === {height}")
        js("window.dispatchEvent(new Event('resize'))")
        wait("""(() => {
            const graph = document.querySelector('#net').getBoundingClientRect();
            const sidebar = document.querySelector('.mobile-sidebar').getBoundingClientRect();
            return !document.querySelector('#activity').hidden &&
                document.querySelector('.sidebar-head > .activity-head') !== null &&
                graph.width > 480 && Math.abs(sidebar.top - graph.top) < 1 &&
                graph.right <= sidebar.left && sidebar.right <= innerWidth &&
                document.documentElement.scrollWidth <= innerWidth;
        })()""")
        assert js('timeVisibility()') == ['visible', 'hidden', 'visible']
        assert not js('recipientOnSameLine()')
        assert js("getComputedStyle(document.querySelector('.activity-head .period-trigger')).display") == 'none'
        assert js("""(() => {
            const stamp = document.querySelector('.activity-mail .repeat-minute');
            return stamp.closest('button').getAttribute('aria-label').endsWith(stamp.textContent);
        })()""")

    client.call('Emulation.setDeviceMetricsOverride', width=390, height=844,
                deviceScaleFactor=2, mobile=True)
    wait("innerWidth === 390 && innerHeight === 844")
    js("window.dispatchEvent(new Event('resize'))")
    wait("document.querySelector('#graph-panel').hidden && !document.querySelector('#activity').hidden")
    assert js("document.querySelector('#activity > .activity-head') !== null")
    assert js('timeVisibility()') == ['visible', 'visible', 'visible']
    assert js('recipientOnSameLine()')
    assert js("getComputedStyle(document.querySelector('.activity-head .period-trigger')).display") != 'none'
    js("document.querySelector('button[data-view=graph]').click()")
    assert js("!document.querySelector('#graph-panel').hidden && document.querySelector('#activity').hidden")


def test_dark_default_and_explicit_light_persist_across_reload(phone):
    client, js, wait, _ = phone
    js('OrreryMobileTerminal.close()')
    client.call('Emulation.setEmulatedMedia', features=[dict(name='prefers-color-scheme', value='light')])
    client.call('Page.reload', ignoreCache=True)
    wait("document.readyState === 'complete' && document.querySelector('#mobile-theme')?.getAttribute('aria-label') === 'Use light theme'")
    assert js("matchMedia('(prefers-color-scheme: light)').matches")
    assert js("document.documentElement.dataset.colorTheme") == 'dark'
    assert js("getComputedStyle(document.documentElement).colorScheme") == 'dark'

    js("document.querySelector('#mobile-theme').click()")
    assert js("getComputedStyle(document.documentElement).colorScheme") == 'light'
    assert js("document.querySelector('meta[name=theme-color]').content") == '#f5efe1'
    assert js("document.querySelector('.mobile-brand img').getAttribute('src')") == '/assets/orbit-light.svg'
    origin = js('performance.timeOrigin')
    client.call('Page.reload', ignoreCache=True)
    wait(f"performance.timeOrigin !== {origin} && "
         "document.querySelector('#mobile-theme')?.getAttribute('aria-label') === 'Use dark theme'")
    assert js("document.documentElement.dataset.colorTheme") == 'light'

    js("document.querySelector('#mobile-theme').click()")
    assert js("getComputedStyle(document.documentElement).colorScheme") == 'dark'
    assert js("document.querySelector('.mobile-brand img').getAttribute('src')") == '/assets/orbit.svg'


def test_workspace_keeps_the_same_reader_and_private_draft_across_layouts(phone):
    from dashboard.mobile_history import project_page
    from test_mobile_steps import fixtures

    client, js, wait, state = phone
    peer = dict(state['snapshot']['agents'][0], name='Peer', act_state='ask')
    state['snapshot']['agents'].append(peer)
    state['snapshot']['graph']['nodes'].append(peer)
    state['snapshot']['graph']['edges'] = [dict(source='SampleAgent', target='Peer', kind='to', count=4)]
    state['workspace'] = True
    state['images'] = True
    state['provider'] = 'codex'
    steps, *_ = project_page(dict(ok=True, provider='codex', events=fixtures('codex')[:-1]))
    state['events'].extend(steps)
    state['targets'] = [
        dict(name=name, choice=choice, pane_label='Window 0 · pane 0', revision='fixture-revision')
        for name, choice in [('SampleAgent', 'fixture-choice'), ('Peer', 'peer-choice')]
    ]

    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("!!document.querySelector('#gsvg [data-name=Peer]')")
    wait("document.querySelector('.conversation-steps-heading')?.textContent === 'Working…'")
    assert js("document.querySelectorAll('.conversation-step').length") == 2
    assert js("document.querySelector('.workspace-home-divider').offsetHeight") == 24
    baseline = js("document.querySelector('.mobile-sidebar').offsetHeight")
    js("document.querySelector('.workspace-home-divider').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp'}))")
    assert js("document.querySelector('.mobile-sidebar').offsetHeight") > baseline
    js("document.querySelector('.workspace-home-divider').dispatchEvent(new KeyboardEvent('keydown',{key:'End'}))")
    assert js("document.querySelector('.mobile-sidebar').offsetHeight") <= 50
    # Enter from the single Reader with this agent preselected and continue on Graph.
    js("document.querySelector('#terminal-sheet [aria-label=\"Add SampleAgent to the workspace\"]').click()")
    wait("document.querySelector('.workspace-picker').open")
    assert js("document.querySelector('.workspace-roster [data-agent=SampleAgent]').checked")
    js("document.querySelector('[aria-label=\"Select from the graph\"]').click()")
    wait("document.querySelector('#mobile-main').classList.contains('workspace-picking')")
    js("document.querySelector('#gsvg [data-name=Peer]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
    assert js("document.querySelector('#gsvg [data-name=Peer]').getAttribute('aria-pressed') === 'true'")
    js("document.querySelector('.workspace-toolbar .workspace-commit').click()")
    js("""window.workspaceFrame = name => document.querySelector('.workspace-card[data-name='+name+'] iframe');
       window.paneDoc = name => workspaceFrame(name)?.contentDocument;""")
    wait("""['SampleAgent','Peer'].every(name => paneDoc(name)?.querySelector('.conversation-mode [data-id=question]')
        && paneDoc(name).querySelector('#terminal-title').textContent===name
        && paneDoc(name).documentElement.dataset.workspaceComposer==='collapsed')""", timeout=12)
    assert js("paneDoc('SampleAgent').querySelectorAll('.conversation-step').length") == 2
    assert js("paneDoc('SampleAgent').querySelector('.conversation-steps-heading').textContent") == 'Working…'
    assert js("paneDoc('Peer').querySelector('.conversation-steps-heading').textContent") == 'Waiting for you'
    assert js("Array.from(document.querySelectorAll('.workspace-reader-frame'), f=>new URL(f.src).search).every(s=>!s)")
    assert js("sessionStorage.getItem('orrery-mobile-view-v1')") is None

    js("paneDoc('SampleAgent').querySelector('.workspace-compose-button').click()")
    wait("workspaceFrame('SampleAgent').dataset.composerOverlay === 'true' && workspaceFrame('SampleAgent').getBoundingClientRect().width > 350")
    assert js("""(() => {
        const frame=workspaceFrame('SampleAgent');
        return {width:frame.offsetWidth, position:getComputedStyle(frame).position, viewport:innerWidth};
    })()""") == {'width':366,'position':'fixed','viewport':390}
    wait("paneDoc('SampleAgent').querySelector('#terminal-image-add').disabled === false")
    js("""(() => {
        const doc=paneDoc('SampleAgent'),field=doc.querySelector('#terminal-text');
        field.value='Private draft for SampleAgent';field.dispatchEvent(new Event('input',{bubbles:true}));
        doc.querySelector('#terminal-image-add').click();
    })()""")
    wait("!paneDoc('SampleAgent').querySelector('.reader-attachment-sources').hidden")
    assert js("""Array.from(paneDoc('SampleAgent').querySelectorAll('.reader-attachment-sources label'),
        label=>[label.textContent,label.querySelector('use').getAttribute('href'),label.control.id])""") == [
            ['', '#icon-photo', 'terminal-image-file'], ['', '#icon-file', 'terminal-file-image']]
    assert js("paneDoc('SampleAgent').querySelector('#terminal-image-file').accept") == 'image/jpeg,image/png,image/webp'
    assert js("paneDoc('SampleAgent').querySelector('#terminal-file-image').accept") == '.jpg,.jpeg,.png,.webp'
    assert js("paneDoc('Peer').querySelector('#terminal-text').value === ''")
    tap_attachment_input(client, js, wait, 'terminal-image-file', frame='SampleAgent')
    js("paneDoc('SampleAgent').getElementById('terminal-image-file').dispatchEvent(new Event('cancel',{bubbles:true}))")
    assert js("""workspaceFrame('SampleAgent').dataset.composerOverlay === 'true'
        && paneDoc('SampleAgent').querySelector('.reader-attachment-sources').hidden
        && paneDoc('SampleAgent').activeElement.id === 'terminal-image-add'""")
    # The image uses the existing upload and review operations of this pane.
    js("""(() => {
        const doc=paneDoc('SampleAgent'), picker=doc.getElementById('terminal-image-file');
        const clip=new DataTransfer();clip.items.add(new File([new Uint8Array([137,80,78,71])],'example.png',{type:'image/png'}));
        picker.files=clip.files;picker.dispatchEvent(new Event('change'));
    })()""")
    wait("!!paneDoc('SampleAgent').querySelector('.reader-image-thumb')")
    assert not js("!!paneDoc('Peer').querySelector('.reader-image-thumb')")
    js("paneDoc('SampleAgent').querySelector('.workspace-collapse-button').click()")
    wait("!workspaceFrame('SampleAgent').hasAttribute('data-composer-overlay')")
    assert js("paneDoc('SampleAgent').querySelector('.workspace-compose-button').dataset.draft === 'true'")

    # Separator endpoints keep the same Reader and draft alive.
    js("document.querySelector('#graph-panel+.workspace-divider').dispatchEvent(new KeyboardEvent('keydown',{key:'End'}))")
    wait("document.querySelector('#mobile-main').classList.contains('workspace-graph-only')")
    assert js("document.querySelector('.workspace-panes').hidden && paneDoc('SampleAgent').querySelector('#terminal-text').value === 'Private draft for SampleAgent'")
    js("document.querySelector('[aria-label=\"Open agent workspace\"]').click()")
    wait("!document.querySelector('.workspace-panes').hidden")
    js("document.querySelector('#graph-panel+.workspace-divider').dispatchEvent(new KeyboardEvent('keydown',{key:'Home'}))")
    wait("document.querySelector('#mobile-main').dataset.terminalLayout === 'full'")
    assert js("getComputedStyle(document.querySelector('#graph-panel')).display === 'none'")
    js("document.querySelector('[aria-label=\"Graph and agent panes\"]').click()")
    client.call('Emulation.setDeviceMetricsOverride', width=1180, height=820, deviceScaleFactor=2, mobile=True,
                screenOrientation={'type':'landscapePrimary','angle':90})
    wait("innerWidth === 1180 && matchMedia('(orientation: landscape)').matches")
    js("window.dispatchEvent(new Event('resize'))")
    wait("document.querySelector('#mobile-main').dataset.splitAxis === 'x'")
    js("document.querySelector('#graph-panel+.workspace-divider').dispatchEvent(new KeyboardEvent('keydown',{key:'Home'}))")
    wait("document.querySelector('#mobile-main').dataset.terminalLayout === 'full'")

    # The Reader's own preview binds a send to this frame's verified target.
    js("paneDoc('SampleAgent').querySelector('.workspace-compose-button').click()")
    wait("paneDoc('SampleAgent').querySelector('#terminal-review').disabled === false")
    js("paneDoc('SampleAgent').querySelector('#terminal-review').click()")
    wait("!!paneDoc('SampleAgent').querySelector('.reader-review')")
    assert state['prepared_target']['name'] == 'SampleAgent'
    assert state['draft'].startswith('Private draft for SampleAgent [image: ')
    assert js("!!paneDoc('SampleAgent').querySelector('.reader-review .reader-image-thumb')")
    assert not any(op == 'commit' for op, _ in state['calls'])
    assert js("!paneDoc('Peer').querySelector('.reader-review')")


def test_successful_receipt_disappears_but_unknown_waits_for_a_status_check(phone):
    _, js, wait, state = phone

    def send(text):
        wait("!document.querySelector('#terminal-text')?.disabled")
        js("""(() => {
            const input=document.getElementById('terminal-text');
            input.value=%r;input.dispatchEvent(new Event('input',{bubbles:true}));
        })()""" % text)
        wait("document.querySelector('#terminal-review')?.disabled === false")
        js("document.getElementById('terminal-review').click()")
        wait("!!document.querySelector('.reader-send-confirm')")
        js("document.querySelector('.reader-send-confirm').click()")

    send('Check the test result.')
    wait("!!document.querySelector('.reader-receipt.accepted')")
    wait("!document.querySelector('.reader-receipt')", timeout=5)
    assert js("!!document.querySelector('.conversation-pending')")

    state['commit_state'] = 'unknown'
    send('Check another test result.')
    wait("!!document.querySelector('.reader-receipt.unknown')")
    js('new Promise(resolve=>setTimeout(resolve,3200))')
    assert js("document.querySelector('.reader-receipt.unknown button').textContent") == 'Check receipt'
    state['receipt_state'] = 'accepted'
    js("document.querySelector('.reader-receipt.unknown button').click()")
    wait("!!document.querySelector('.reader-receipt.accepted')")
    wait("!document.querySelector('.reader-receipt')", timeout=5)
    assert [op for op, _ in state['calls'] if op in {'commit','status'}] == ['commit','commit','status']


@pytest.mark.parametrize('phone', [(820, 1180), (1180, 820)], indirect=True)
def test_model_list_opens_inside_tablet_sheet(phone):
    client, js, wait, state = phone
    harnesses = [
        dict(id='codex', key='codex', label='Codex', efforts=['low', 'medium', 'high'],
             default_effort='medium', default=True, effort_required=False),
        dict(id='dsh', key='dsh', label='DSH', efforts=[], default_effort='', default=False),
    ]
    state['spawn_catalog'] = dict(
        configurable=True,
        models=[dict(provider='fixture', provider_label='Fixture', model=f'Model {i}',
                     default=i == 1, harnesses=harnesses) for i in range(1, 13)],
        directories=[dict(path='/workspace/projects/design/', label='/workspace/projects/design/'),
                     dict(path='~', label='Home directory')],
    )
    js("OrreryMobileTerminal.close();document.getElementById('mobile-new-agent').click()")
    wait("!document.getElementById('spawn-editor').hidden")
    assert js("document.querySelector('.spawn-folder .om-select-slot:not([aria-hidden])').textContent") == 'design'
    js("document.querySelector('.spawn-folder .om-select-trigger').click()")
    wait("document.querySelector('.spawn-folder .om-select-option:last-child').getBoundingClientRect().height > 0")
    js("document.querySelector('.spawn-folder .om-select-option:last-child').click()")
    assert js("document.querySelector('.spawn-folder .om-select-slot:not([aria-hidden])').textContent") == '~'
    js("document.getElementById('spawn-model-chip').click()")
    js("document.querySelector('#spawn-model + .om-select .om-select-trigger').click()")
    wait("""(() => {
        const sheet = document.getElementById('spawn-settings');
        const host = document.querySelector('#spawn-model + .om-select');
        const menu = host.querySelector('.om-select-menu');
        const popup = host.querySelector('.om-select-popup').getBoundingClientRect();
        const bounds = sheet.getBoundingClientRect(), viewport = visualViewport;
        const trigger = host.querySelector('.om-select-trigger').getBoundingClientRect();
        const row = menu.querySelectorAll('[role=option]')[2].getBoundingClientRect();
        const hit = document.elementFromPoint(row.x + row.width/2, row.y + row.height/2);
        return bounds.top >= viewport.offsetTop && bounds.bottom <= viewport.offsetTop + viewport.height
            && popup.top >= trigger.bottom - 1 && popup.bottom <= bounds.bottom
            && popup.height >= menu.clientHeight && menu.scrollHeight > menu.clientHeight
            && hit?.closest('[role=option]')?.textContent === 'Model 3';
    })()""")
    point = js("""(() => {
        const row = document.querySelectorAll('#spawn-model + .om-select [role=option]')[2].getBoundingClientRect();
        return {x: row.x + row.width/2, y: row.y + row.height/2};
    })()""")
    client.call('Emulation.setTouchEmulationEnabled', enabled=True)
    client.call('Input.dispatchTouchEvent', type='touchStart', touchPoints=[point])
    client.call('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])
    wait("document.getElementById('spawn-model-name').textContent === 'Model 3'")
    assert js("document.getElementById('spawn-directory').value") == '1'


@pytest.mark.parametrize('phone', [(390, 844), (820, 1180), (1180, 820)], indirect=True)
def test_file_picker_adds_a_document_to_existing_input_review(phone):
    client, js, wait, state = phone
    state['files'] = True
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('#terminal-image-add')?.disabled === false")
    assert js("""(() => {
        const file = document.querySelector('#terminal-file-image');
        const photo = document.querySelector('#terminal-image-file');
        return !file.hasAttribute('accept') && !file.hasAttribute('capture')
            && photo.accept === 'image/jpeg,image/png,image/webp';
    })()""")
    js("document.querySelector('#terminal-image-add').click()")
    assert js("document.querySelector('label[for=terminal-file-image]').textContent") == ''
    assert js("document.querySelector('label[for=terminal-image-file]').hidden")
    tap_attachment_input(client, js, wait, 'terminal-file-image')
    js("document.getElementById('terminal-file-image').dispatchEvent(new Event('cancel',{bubbles:true}))")
    assert js("""document.getElementById('terminal-sheet').open
        && document.querySelector('.reader-attachment-sources').hidden
        && document.activeElement.id === 'terminal-image-add'""")

    def choose_file():
        js("""(() => {
            const input=document.getElementById('terminal-file-image'), selection=new DataTransfer();
            selection.items.add(new File(['# Example\\n'],'Notes <v2>.MD',{type:'text/markdown'}));
            input.files=selection.files; input.dispatchEvent(new Event('change'));
        })()""")

    if js('innerWidth') > 720:
        target = next(payload for op,payload in state['calls'] if op == 'preview')
        state['targets'] = [dict(choice='resumed-choice', name='SampleAgent',
                                 pane_label='Window 0 · pane 0', revision='fixture-revision')]
        assert target['choice'] != 'resumed-choice'
        js("""Object.defineProperty(document,'hidden',{configurable:true,value:true});
              document.dispatchEvent(new Event('visibilitychange'));""")
    choose_file()
    if js('document.hidden'):
        assert not any(op=='file' and payload.get('file') for op,payload in state['calls'])
        js("""delete document.hidden;document.dispatchEvent(new Event('visibilitychange'));""")
    wait("document.querySelector('.reader-attachment-label')?.textContent === 'Notes <v2>.MD'")
    wait("document.querySelector('#terminal-review')?.disabled === false")
    uploads = [payload for op,payload in state['calls'] if op=='file' and payload.get('file')]
    assert [payload['file'] for payload in uploads] == [
        {'format':'md','data':'IyBFeGFtcGxlCg=='},
    ]
    if js('innerWidth') > 720:
        assert uploads[0]['choice'] == 'resumed-choice'
    assert not any(op=='prepare' or op=='commit' for op,_ in state['calls'])
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.reader-review .reader-attachment-label')?.textContent === 'Notes <v2>.MD'")
    assert state['draft'] == "[file: '/workspace/.orrery-input-files/"+'b'*32+".md']"
    assert not any(op=='commit' for op,_ in state['calls'])
    js("Array.from(document.querySelectorAll('.reader-review button')).find(b=>b.textContent==='Edit').click()")
    wait("!!document.querySelector('button[aria-label=\"Remove Notes <v2>.MD\"]')")
    js("document.querySelector('button[aria-label=\"Remove Notes <v2>.MD\"]').click()")
    assert js("!document.querySelector('.reader-file-mark') && document.querySelector('#terminal-review').disabled")

    if js('innerWidth') > 720:
        js("""Object.defineProperty(document,'hidden',{configurable:true,value:true});
              document.dispatchEvent(new Event('visibilitychange'));""")
        choose_file()
        state['targets'][0]['revision'] = 'different-revision'
        js("""delete document.hidden;document.dispatchEvent(new Event('visibilitychange'));""")
        wait("document.querySelector('#terminal-sheet').innerText.includes('Pane changed.')")
        assert [payload for op,payload in state['calls'] if op=='file' and payload.get('file')] == uploads
