import json
import time
import pytest
from mobile_reader_browser import phone

HIDE="window.pwaHidden=true;Object.defineProperty(document,'hidden',{configurable:true,get:()=>pwaHidden});document.dispatchEvent(new Event('visibilitychange'))"
SHOW="pwaHidden=false;document.dispatchEvent(new Event('visibilitychange'))"


def test_loading_preview_resumes_after_background(phone):
    _,js,wait,state=phone
    state['events'].append(dict(id='file-extra',kind='tool',role='assistant',text='Read',files=[dict(ref='a'*32,kind='file',label='notes.md')],ts=''))
    wait("document.querySelector('.previewable')!==null",8)
    js("window.realFetch=fetch;window.releasePreview=null;window.fetch=async(...a)=>{const r=await realFetch(...a);if(String(a[0]).startsWith('/view/conversation/preview'))await new Promise((resolve,reject)=>{releasePreview=resolve;a[1].signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true})});return r};document.querySelector('.previewable').click()")
    wait('releasePreview!==null')
    js(HIDE)
    js("window.fetch=realFetch;"+SHOW)
    wait("document.querySelector('#terminal-text')?.disabled===false")
    state['events'].append(dict(id='after-resume',kind='text',role='assistant',text='Healthy record after resume',ts=''))
    wait("document.querySelector('[data-id=after-resume]')!==null",8)
    value=js("document.querySelector('.conversation-preview-status').textContent")
    print('PREVIEW_AFTER_HEALTHY_POLL',value)
    assert value!='Loading preview…','aborted preview still looks busy and is never restarted'
    assert js("document.querySelector('.conversation-preview-text')?.textContent")=='File contents'
    assert len([url for url in state['get_calls'] if url.startswith('/view/conversation/preview')])==2


def test_reload_does_not_silently_replace_fresh_identity_with_same_name(phone):
    client,js,wait,state=phone
    identity=dict(mail_instance_id='a'*32,mail_agent_id=7)
    state['snapshot']['agents'][0].update(identity)
    state['targets']=[dict(choice='fresh-choice',name='SampleAgent',pane_label='Window 0 · pane 0',revision='fixture-revision',**identity)]
    client.call('Page.addScriptToEvaluateOnNewDocument',source="""const realFetch=window.fetch;window.fetch=async(url,options)=>{const r=await realFetch(url,options);if(url==='/view/terminal'){const v=await r.json();v.api=34;v.supported='fresh-managed-pane';return new Response(JSON.stringify(v),{headers:{'Content-Type':'application/json'}})}return r};""")
    client.call('Page.reload',ignoreCache=True)
    wait("typeof OrreryMobileTerminal!=='undefined'&&document.querySelector('body>header>span').textContent==='History · Terminal'&&OrreryMobileIdentity.forName('SampleAgent')!==null")
    # Select the original fresh identity explicitly before testing a reload.
    js("OrreryMobileTerminal.open('SampleAgent',OrreryMobileIdentity.forName('SampleAgent'))")
    wait("document.querySelector('#terminal-text')?.disabled===false")
    assert [p for op,p in state['calls'] if op=='targets'][-1]['mail_agent_id']==7
    identity2=dict(mail_instance_id='b'*32,mail_agent_id=8)
    state['snapshot']['agents'][0].update(identity2)
    state['targets']=[dict(choice='different-agent-choice',name='SampleAgent',pane_label='Window 0 · pane 0',revision='different-agent-revision',**identity2)]
    before=len(state['calls'])
    client.call('Page.reload',ignoreCache=True)
    wait("typeof OrreryMobileTerminal!=='undefined'&&document.querySelector('body>header>span').textContent==='History · Terminal'")
    time.sleep(.4)
    requests=[p for op,p in state['calls'][before:] if op=='targets']
    print('AUTO_REOPEN_TARGETS',requests)
    assert not requests,'reload opened a new I/A using only the old display name'


def test_pagehide_clears_draft_and_receipt_but_close_removes_resume_key(phone):
    _,js,wait,state=phone
    js("document.querySelector('#terminal-text').value='Ephemeral text';document.querySelector('#terminal-text').dispatchEvent(new Event('input'));dispatchEvent(new PageTransitionEvent('pagehide'))")
    wait("!document.querySelector('#terminal-sheet').open")
    assert js("document.querySelector('#terminal-sheet').textContent===''&&sessionStorage.getItem('orrery-mobile-view-v1')!==null")
    js("OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('#terminal-text')!==null")
    assert js("document.querySelector('#terminal-text').value==='' ")
    js('OrreryMobileTerminal.close()')
    assert js("sessionStorage.getItem('orrery-mobile-view-v1')===null")


def test_fresh_saved_identity_is_not_reopened_as_legacy_by_name(phone):
    client,js,wait,state=phone
    # A prior fresh launch's persisted selection; this page now serves legacy API31.
    js("dispatchEvent(new PageTransitionEvent('pagehide'));sessionStorage.setItem('orrery-mobile-view-v1',JSON.stringify({agent:'SampleAgent',tab:'terminal',mail_instance_id:'a'.repeat(32),mail_agent_id:7}))")
    before=len(state['calls'])
    client.call('Page.reload',ignoreCache=True)
    wait("typeof OrreryMobileTerminal!=='undefined'&&document.querySelector('body>header>span').textContent==='History · Terminal'")
    time.sleep(.3)
    requests=[p for op,p in state['calls'][before:] if op=='targets']
    print('FRESH_TO_LEGACY',requests)
    assert not requests,'fresh I/A was discarded and the same name was reopened through legacy'


@pytest.mark.parametrize('which',['instance','agent'])
def test_one_changed_fresh_identity_field_prevents_restore(phone,which):
    client,js,wait,state=phone
    saved=dict(agent='SampleAgent',tab='conversation',mail_instance_id='a'*32,mail_agent_id=7)
    current=dict(mail_instance_id='b'*32 if which=='instance' else 'a'*32,mail_agent_id=8 if which=='agent' else 7)
    state['snapshot']['agents'][0].update(current)
    state['targets']=[dict(choice='fresh-choice',name='SampleAgent',pane_label='Window 0 · pane 0',revision='fixture-revision',**current)]
    js('dispatchEvent(new PageTransitionEvent("pagehide"));sessionStorage.setItem("orrery-mobile-view-v1",'+json.dumps(json.dumps(saved))+')')
    client.call('Page.addScriptToEvaluateOnNewDocument',source="""const realFetch=fetch;window.fetch=async(u,o)=>{const r=await realFetch(u,o);if(u!=='/view/terminal')return r;const d=await r.json();d.api=34;d.supported='fresh-managed-pane';return new Response(JSON.stringify(d),{headers:{'Content-Type':'application/json'}})};""")
    before=len(state['calls']);client.call('Page.reload',ignoreCache=True)
    wait("typeof OrreryMobileTerminal!=='undefined'&&OrreryMobileIdentity?.forName('SampleAgent')!==null&&document.querySelector('body>header>span').textContent==='History · Terminal'")
    time.sleep(.2)
    assert not js("document.querySelector('#terminal-sheet').open")
    assert not any(op=='targets' for op,p in state['calls'][before:])
    assert js("sessionStorage.getItem('orrery-mobile-view-v1')===null")


@pytest.mark.parametrize('close',['preview','unmount'])
def test_retried_preview_late_response_cannot_restore_after_close(phone,close):
    _,js,wait,state=phone
    state['events'].append(dict(id='edge-file',kind='tool',role='assistant',text='Read',files=[dict(ref='a'*32,kind='file',label='edge.md')],ts=''))
    wait("document.querySelector('.previewable')!==null",8)
    js("document.querySelector('.conversation-tool-group')?.click();window.realFetch=fetch;window.previewReleases=[];window.fetch=async(...a)=>{const r=await realFetch(...a);if(String(a[0]).startsWith('/view/conversation/preview'))await new Promise(resolve=>previewReleases.push(resolve));return r};document.querySelector('.previewable').focus();document.querySelector('.previewable').click()")
    wait('previewReleases.length===1');js(HIDE);js(SHOW);wait('previewReleases.length===2')
    if close=='preview':js("document.querySelector('.conversation-preview-close').click()")
    else:js('OrreryMobileTerminal.close()')
    js('previewReleases.forEach(f=>f());new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
    assert js("document.querySelector('.conversation-preview')===null")
    if close=='preview':assert js("document.activeElement.classList.contains('previewable')")
    else:assert js("document.querySelector('#terminal-sheet').textContent===''")


def test_completed_preview_stays_and_is_not_refetched(phone):
    _,js,wait,state=phone
    state['events'].append(dict(id='completed-file',kind='tool',role='assistant',text='Read',files=[dict(ref='a'*32,kind='file',label='done.md')],ts=''))
    wait("document.querySelector('.previewable')!==null",8)
    js("document.querySelector('.previewable').click()")
    wait("document.querySelector('.conversation-preview-text')!==null")
    js("window.keptPreview=document.querySelector('.conversation-preview')")
    before=len([u for u in state['get_calls'] if u.startswith('/view/conversation/preview')])
    js(HIDE);js(SHOW)
    wait("document.querySelector('#terminal-text')?.disabled===false")
    assert js("document.querySelector('.conversation-preview')===keptPreview&&keptPreview.querySelector('.conversation-preview-text').textContent==='File contents'")
    assert len([u for u in state['get_calls'] if u.startswith('/view/conversation/preview')])==before


def test_fresh_saved_identity_is_checked_when_phone_sending_is_disabled(phone):
    client,js,wait,state=phone
    state['snapshot']['agents'][0].update(mail_instance_id='b'*32,mail_agent_id=8)
    js("dispatchEvent(new PageTransitionEvent('pagehide'));sessionStorage.setItem('orrery-mobile-view-v1',JSON.stringify({agent:'SampleAgent',tab:'conversation',mail_instance_id:'a'.repeat(32),mail_agent_id:7}))")
    client.call('Page.addScriptToEvaluateOnNewDocument',source="""const realFetch=fetch;window.fetch=async(u,o)=>{const r=await realFetch(u,o);if(u!=='/view/terminal')return r;const d=await r.json();d.api=34;d.supported='fresh-managed-pane';d.enabled=false;return new Response(JSON.stringify(d),{headers:{'Content-Type':'application/json'}})};""")
    client.call('Page.reload',ignoreCache=True)
    wait("typeof OrreryMobileTerminal!=='undefined'&&document.querySelector('body>header>span').textContent==='History enabled'&&OrreryMobileIdentity.forName('SampleAgent')!==null")
    time.sleep(.3)
    print('READ_ONLY_RESTORED',js("document.querySelector('#terminal-sheet').open"),'CURRENT_ID',js("OrreryMobileIdentity.forName('SampleAgent')"))
    assert not js("document.querySelector('#terminal-sheet').open"),'disabling phone input bypassed the saved fresh identity check'


@pytest.mark.parametrize('mode',['disabled','legacy'])
@pytest.mark.parametrize('roster',['same','other','no_identity'])
def test_saved_identity_rule_survives_mode_changes_and_repeated_reload(phone,mode,roster):
    client,js,wait,state=phone
    saved=dict(agent='SampleAgent',tab='conversation',mail_instance_id='a'*32,mail_agent_id=7)
    if roster!='no_identity':
        state['snapshot']['agents'][0].update(mail_instance_id=('a' if roster=='same' else 'b')*32,mail_agent_id=7 if roster=='same' else 8)
    js('dispatchEvent(new PageTransitionEvent("pagehide"));sessionStorage.setItem("orrery-mobile-view-v1",'+json.dumps(json.dumps(saved))+')')
    if mode=='disabled':
        client.call('Page.addScriptToEvaluateOnNewDocument',source="""const realFetch=fetch;window.fetch=async(u,o)=>{const r=await realFetch(u,o);if(u!=='/view/terminal')return r;const d=await r.json();d.api=34;d.supported='fresh-managed-pane';d.enabled=false;return new Response(JSON.stringify(d),{headers:{'Content-Type':'application/json'}})};""")
    for _ in range(2):
        before=len(state['calls'])
        client.call('Page.reload',ignoreCache=True)
        wait("typeof OrreryMobileTerminal!=='undefined'&&document.querySelector('body>header>span').textContent==="+json.dumps('History enabled' if mode=='disabled' else 'History · Terminal')+"&&document.querySelector('#snapshot-note').textContent.startsWith('Snapshot observed')")
        if roster=='same':
            wait("document.querySelector('#terminal-sheet').open")
            assert js("JSON.parse(sessionStorage.getItem('orrery-mobile-view-v1'))")==saved,'re-saving must retain I/A even with sending off or legacy API'
        else:
            time.sleep(.2)
            assert not js("document.querySelector('#terminal-sheet').open")
            assert not any(op=='targets' for op,_ in state['calls'][before:])
