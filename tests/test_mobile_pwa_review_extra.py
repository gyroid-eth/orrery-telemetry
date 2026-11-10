import json
import time
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
