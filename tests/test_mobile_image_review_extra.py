import json
from mobile_reader_browser import phone


def attach(js):
    js("(()=>{const files=new DataTransfer();files.items.add(new File([Uint8Array.from([137,80,78,71])],'fixture.png',{type:'image/png'}));const f=document.querySelector('#terminal-image-file');f.files=files.files;f.dispatchEvent(new Event('change'));})()")


def test_one_failed_image_probe_recovers_on_healthy_poll(phone):
    _,js,wait,state=phone
    state['images']=True
    js("OrreryMobileTerminal.close();window.realFetch=fetch;window.didFailProbe=false;window.fetch=(...a)=>{if(String(a[0]).endsWith('/image')&&!didFailProbe){didFailProbe=true;return Promise.reject(Error('temporary network failure'))}return realFetch(...a)};OrreryMobileTerminal.open('SampleAgent')")
    wait('didFailProbe')
    previews=sum(op=='preview' for op,_ in state['calls'])
    wait("document.querySelector('#terminal-text')?.disabled===false")
    # Two successful common polls occur after the one-shot failure.
    import time
    end=time.monotonic()+9
    while sum(op=='preview' for op,_ in state['calls'])<previews+2 and time.monotonic()<end:
        time.sleep(.05)
    assert sum(op=='preview' for op,_ in state['calls'])>=previews+2
    print('HEALTHY_PREVIEWS',sum(op=='preview' for op,_ in state['calls']),'IMAGE_CALLS',sum(op=='image' for op,_ in state['calls']))
    assert js("document.querySelector('#terminal-image-add').hidden===false"),'one transient probe failure disabled images until the Reader is reopened'


def test_late_upload_is_not_applied_after_revision_change(phone):
    _,js,wait,state=phone
    state['images']=True
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('#terminal-image-add')?.hidden===false")
    js("window.realFetch=fetch;window.releaseImage=null;window.fetch=async(...a)=>{const r=await realFetch(...a);if(String(a[0]).endsWith('/image')&&JSON.parse(a[1].body).image!==null)return new Promise(resolve=>releaseImage=()=>resolve(r));return r}")
    attach(js);wait('releaseImage!==null')
    state['targets']=[dict(choice='fixture-choice',name='SampleAgent',pane_label='Window 0 · pane 0',revision='changed-revision')]
    wait("document.querySelector('#terminal-sheet').innerText.includes('Pane changed')",8)
    js('releaseImage();void 0')
    wait("document.querySelector('.reader-image-thumb')===null")
    assert not any(op in {'prepare','commit'} for op,_ in state['calls'])


def test_unknown_image_send_keeps_receipt_and_does_not_retry(phone):
    _,js,wait,state=phone
    state['images']=True
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('#terminal-image-add')?.hidden===false")
    attach(js);wait("document.querySelector('.reader-image-thumb')!==null")
    js("window.realFetch=fetch;window.fetch=async(...a)=>{const r=await realFetch(...a);if(String(a[0]).endsWith('/commit'))throw Error('lost receipt');return r};document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.reader-review')!==null")
    js("const s=[...document.querySelectorAll('button')].find(b=>b.textContent==='Send once');s.click();s.click()")
    wait("document.querySelector('.reader-receipt.unknown')!==null")
    assert js("document.querySelector('#terminal-image-add').disabled&&document.querySelector('#terminal-text').disabled")
    state['targets']=[dict(choice='fixture-choice',name='SampleAgent',pane_label='Window 0 · pane 0',revision='changed-revision')]
    wait("document.querySelector('.terminal-message').textContent.includes('Receipt retained')",8)
    js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Check receipt').click()")
    wait("document.querySelector('.terminal-message').textContent.includes('Receipt unavailable')")
    assert sum(op=='commit' for op,_ in state['calls'])==1
    assert sum(op=='status' for op,_ in state['calls'])==1
    assert '[image:' in state['draft']
