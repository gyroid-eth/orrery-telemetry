"""The responsive layout and theme use the packaged mobile page."""
from mobile_reader_browser import phone  # noqa: F401


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
    assert js("""Array.from(paneDoc('SampleAgent').querySelectorAll('.reader-attachment-sources button'),
        b=>[b.textContent,b.querySelector('use').getAttribute('href')])""") == [['', '#icon-photo'], ['', '#icon-file']]
    assert js("paneDoc('SampleAgent').querySelector('#terminal-image-file').accept") == 'image/jpeg,image/png,image/webp'
    assert js("paneDoc('SampleAgent').querySelector('#terminal-file-image').accept") == '.jpg,.jpeg,.png,.webp'
    assert js("paneDoc('Peer').querySelector('#terminal-text').value === ''")
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


def test_file_picker_adds_a_document_to_existing_input_review(phone):
    _, js, wait, state = phone
    state['files'] = True
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('#terminal-file-image')?.accept === '.pdf,.txt,.md,.csv,.json'")
    wait("document.querySelector('#terminal-image-add')?.disabled === false")
    js("document.querySelector('#terminal-image-add').click()")
    assert js("document.querySelector('.reader-attachment-sources button[aria-label=\"Choose a file\"]').textContent") == ''
    assert js("document.querySelector('.reader-attachment-sources button[aria-label=\"Choose a photo\"]').hidden")

    def choose_file():
        js("""(() => {
            const input=document.getElementById('terminal-file-image'), selection=new DataTransfer();
            selection.items.add(new File(['# Example\\n'],'Notes <v2>.MD',{type:'text/markdown'}));
            input.files=selection.files; input.dispatchEvent(new Event('change'));
        })()""")
        wait("document.querySelector('.reader-attachment-label')?.textContent === 'Notes <v2>.MD'")
        wait("document.querySelector('#terminal-review')?.disabled === false")

    choose_file()
    assert [payload['file'] for op,payload in state['calls'] if op=='file' and payload.get('file')] == [
        {'format':'md','data':'IyBFeGFtcGxlCg=='},
    ]
    assert not any(op=='prepare' or op=='commit' for op,_ in state['calls'])
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.reader-review .reader-attachment-label')?.textContent === 'Notes <v2>.MD'")
    assert state['draft'] == "[file: '/workspace/.orrery-input-files/"+'b'*32+".md']"
    assert not any(op=='commit' for op,_ in state['calls'])
    js("Array.from(document.querySelectorAll('.reader-review button')).find(b=>b.textContent==='Edit').click()")
    wait("!!document.querySelector('button[aria-label=\"Remove Notes <v2>.MD\"]')")
    js("document.querySelector('button[aria-label=\"Remove Notes <v2>.MD\"]').click()")
    assert js("!document.querySelector('.reader-file-mark') && document.querySelector('#terminal-review').disabled")
