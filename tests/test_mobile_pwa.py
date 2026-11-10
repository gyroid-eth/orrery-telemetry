"""Authenticated PWA assets and real Chromium installability / resume checks."""
import http.client
import base64
import json
import os
import time
from pathlib import Path

import pytest

from dashboard import mobile_relay as relay
from test_mobile_relay import Store, serving
from mobile_reader_browser import phone  # noqa: F401

ROOT=Path(__file__).resolve().parents[1]
ASSETS=['/manifest.webmanifest','/icons/orbit-192.png','/icons/orbit-512.png','/icons/orbit-maskable-512.png','/icons/apple-touch-icon.png']


@pytest.mark.parametrize('path',ASSETS)
def test_manifest_and_icons_pass_the_existing_owner_admission(path):
    cfg=relay.Config('owner@example.test','https://viewer.example.ts.net')
    server=relay.make_server(cfg,store=Store(),test_port=0)
    assert server.server_address[0]=='127.0.0.1'
    with serving(server) as port:
        headers={'Host':'viewer.example.ts.net','Tailscale-User-Login':'owner@example.test',
                 'Sec-Fetch-Site':'same-origin','Sec-Fetch-Mode':'cors' if path.endswith('webmanifest') else 'no-cors',
                 'Sec-Fetch-Dest':'manifest' if path.endswith('webmanifest') else 'image'}
        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
        try:
            connection.request('GET',path,headers=headers)
            response=connection.getresponse();body=response.read()
            assert response.status==200
            assert response.getheader('Cache-Control')=='no-store'
            assert "manifest-src 'self'" in response.getheader('Content-Security-Policy')
            if path.endswith('webmanifest'):
                assert response.getheader('Content-Type').split(';')[0]=='application/manifest+json'
                value=json.loads(body)
                assert value['name']==value['short_name']=='ORRERY'
                assert value['start_url']==value['scope']=='/' and value['display']=='standalone'
            else:
                from PIL import Image
                import io
                assert response.getheader('Content-Type')=='image/png'
                with Image.open(io.BytesIO(body)) as image:
                    side=192 if '192' in path else 180 if 'apple' in path else 512
                    assert image.size==(side,side)
        finally:connection.close()


@pytest.mark.parametrize('case',['host','origin','identity_missing','identity_other','cross_site'])
def test_pwa_assets_are_not_public(case):
    server=relay.make_server(relay.Config('owner@example.test','https://viewer.example.ts.net'),store=Store(),test_port=0)
    with serving(server) as port:
        headers={'Host':'viewer.example.ts.net','Tailscale-User-Login':'owner@example.test','Origin':'https://viewer.example.ts.net'}
        if case=='host':headers['Host']='outsider.example.test'
        if case=='origin':headers['Origin']='https://outsider.example.test'
        if case=='identity_missing':del headers['Tailscale-User-Login']
        if case=='identity_other':headers['Tailscale-User-Login']='outsider@example.test'
        if case=='cross_site':headers.update({'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'})
        for path in ASSETS:
            connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
            try:
                connection.request('GET',path,headers=headers)
                response=connection.getresponse();response.read()
                assert response.status==403
            finally:connection.close()


def test_index_links_installable_manifest():
    text=(ROOT/'dashboard/mobile/index.html').read_text()
    assert 'rel="manifest" href="/manifest.webmanifest"' in text
    assert 'viewport-fit=cover' in text
    assert 'apple-mobile-web-app-capable' in text


def test_chromium_manifest_and_installability_without_service_worker(phone):
    client,js,wait,state=phone
    result=client.call('Page.getAppManifest')
    assert not result.get('errors'),result
    assert json.loads(result.get('data') or '{}').get('display')=='standalone',result
    result=client.call('Page.getInstallabilityErrors')
    assert result['installabilityErrors']==[],result
    assert js("navigator.serviceWorker.getRegistrations().then(values=>values.length)")==0


@pytest.mark.parametrize('tab',['conversation','terminal'])
def test_background_keeps_view_draft_and_position_then_rechecks_pane(phone,tab):
    client,js,wait,state=phone
    state['preview_text']='\n'.join('line '+str(i) for i in range(100))
    if tab=='terminal':
        js("document.querySelector('[aria-label=Terminal]').click()")
        wait("document.querySelector('#terminal-output')?.textContent.includes('line 99')",8)
    surface='#terminal-output' if tab=='terminal' else '.conversation-list'
    js("document.querySelector('#terminal-text').value='Memory-only draft';document.querySelector('#terminal-text').dispatchEvent(new Event('input'));document.querySelector("+json.dumps(surface)+").scrollTop=50")
    before=js("document.querySelector("+json.dumps(surface)+").scrollTop")
    js("window.pwaHidden=true;Object.defineProperty(document,'hidden',{configurable:true,get:()=>pwaHidden});document.dispatchEvent(new Event('visibilitychange'))")
    assert js("document.querySelector('#terminal-sheet').open")
    calls=len(state['calls'])+len(state['get_calls'])
    time.sleep(5.2)
    assert len(state['calls'])+len(state['get_calls'])==calls
    js("pwaHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('#terminal-text')?.value==='Memory-only draft'")
    wait("document.querySelector('#terminal-review')?.disabled===false")
    assert js("document.querySelector('[aria-label="+('Terminal' if tab=='terminal' else 'Conversation')+"]').getAttribute('aria-pressed')")== 'true'
    assert abs(js("document.querySelector("+json.dumps(surface)+").scrollTop")-before)<=1
    state['targets']=[dict(choice='fixture-choice',name='SampleAgent',pane_label='Window 0 · pane 0',revision='changed-revision')]
    js("pwaHidden=true;document.dispatchEvent(new Event('visibilitychange'));pwaHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('#terminal-sheet').innerText.includes('Pane changed')",8)


def test_reload_restores_only_agent_and_tab(phone):
    client,js,wait,state=phone
    js("document.querySelector('[aria-label=Terminal]').click();document.querySelector('#terminal-text').value='Do not persist this';document.querySelector('#terminal-text').dispatchEvent(new Event('input'))")
    stored=js("JSON.parse(sessionStorage.getItem('orrery-mobile-view-v1'))")
    assert stored=={'agent':'SampleAgent','tab':'terminal'}
    client.call('Page.reload',ignoreCache=True)
    wait("document.querySelector('#terminal-sheet').open&&document.querySelector('[aria-label=Terminal]')?.getAttribute('aria-pressed')==='true'")
    wait("document.querySelector('#terminal-text')!==null")
    assert js("document.querySelector('#terminal-text').value")==''


def test_reload_restores_the_same_fresh_identity_from_the_current_roster(phone):
    client,js,wait,state=phone
    identity=dict(mail_instance_id='a'*32,mail_agent_id=7)
    state['snapshot']['agents'][0].update(identity)
    state['targets']=[dict(choice='fresh-choice',name='SampleAgent',pane_label='Window 0 · pane 0',revision='fixture-revision',**identity)]
    client.call('Page.addScriptToEvaluateOnNewDocument',source="""
      const realFetch=window.fetch;
      window.fetch=async(url,options)=>{
        const response=await realFetch(url,options);
        if(url==='/view/terminal'){
          const data=await response.json();data.api=34;data.supported='fresh-managed-pane';
          return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
        }
        return response;
      };
    """)
    client.call('Page.reload',ignoreCache=True)
    wait("typeof OrreryMobileTerminal!=='undefined'&&document.querySelector('body>header>span').textContent==='History · Terminal'&&OrreryMobileIdentity.forName('SampleAgent')!==null")
    # A legacy name-only selection still restores by name, then retains the
    # current roster identity for subsequent reloads.
    wait("document.querySelector('#terminal-sheet').open&&document.querySelector('#terminal-text')!==null")
    js("document.querySelector('#terminal-text').value='Fresh identity';document.querySelector('#terminal-text').dispatchEvent(new Event('input'))")
    wait("document.querySelector('#terminal-review').disabled===false")
    payload=[value for op,value in state['calls'] if op=='targets'][-1]
    assert all(payload[k]==v for k,v in identity.items())
    stored=js("JSON.parse(sessionStorage.getItem('orrery-mobile-view-v1'))")
    assert stored==dict(agent='SampleAgent',tab='conversation',**identity)
    client.call('Page.reload',ignoreCache=True)
    wait("document.querySelector('#terminal-sheet').open&&document.querySelector('#terminal-text')?.disabled===false")
    assert js("document.querySelector('#terminal-text').value")==''


def test_background_renews_choice_and_expires_confirmation_without_sending(phone):
    _,js,wait,state=phone
    js("document.querySelector('#terminal-text').value='Keep this draft';document.querySelector('#terminal-text').dispatchEvent(new Event('input'))")
    wait("document.querySelector('#terminal-review').disabled===false")
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.reader-review')!==null")
    js("window.pwaHidden=true;Object.defineProperty(document,'hidden',{configurable:true,get:()=>pwaHidden});document.dispatchEvent(new Event('visibilitychange'))")
    state['targets']=[dict(choice='renewed-choice',name='SampleAgent',pane_label='Window 0 · pane 0',revision='fixture-revision')]
    js("window.realNow=Date.now;Date.now=()=>realNow()+31000;pwaHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('#terminal-text')?.value==='Keep this draft'&&document.querySelector('#terminal-review').disabled===false")
    assert js("document.querySelector('.reader-review')===null")
    assert not any(op=='commit' for op,_ in state['calls'])
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.reader-review')!==null")
    assert [payload for op,payload in state['calls'] if op=='prepare'][-1]['choice']=='renewed-choice'


def test_background_during_commit_preserves_unknown_receipt_and_never_retries(phone):
    _,js,wait,state=phone
    js("""window.realFetch=fetch;window.fetch=async(url,options)=>{
      const response=await realFetch(url,options);
      if(String(url).endsWith('/commit')){
        window.commitHeld=true;
        await new Promise((resolve,reject)=>{
          window.releaseCommit=resolve;
          options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
        });
      }
      return response;
    };
    document.querySelector('#terminal-text').value='Send once';document.querySelector('#terminal-text').dispatchEvent(new Event('input'))""")
    wait("document.querySelector('#terminal-review').disabled===false")
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.reader-review')!==null")
    js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Send once').click()")
    wait("window.commitHeld===true")
    js("window.pwaHidden=true;Object.defineProperty(document,'hidden',{configurable:true,get:()=>pwaHidden});document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('.reader-receipt.unknown')!==null")
    js("pwaHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('.reader-receipt.unknown')!==null")
    assert js("document.querySelector('#terminal-text').disabled")
    assert js("document.querySelector('.reader-input-target').disabled")
    js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Check receipt').click()")
    wait("document.querySelector('.terminal-message').textContent.includes('Receipt unavailable')")
    assert sum(op=='commit' for op,_ in state['calls'])==1
    assert sum(op=='status' for op,_ in state['calls'])==1


def test_late_prepare_after_background_cannot_restore_an_unsent_confirmation(phone):
    _,js,wait,state=phone
    js("""window.realFetch=fetch;window.fetch=async(url,options)=>{
      const response=await realFetch(url,options);
      if(String(url).endsWith('/prepare'))await new Promise(resolve=>window.releasePrepare=resolve);
      return response;
    };
    document.querySelector('#terminal-text').value='Unsent draft';document.querySelector('#terminal-text').dispatchEvent(new Event('input'))""")
    wait("document.querySelector('#terminal-review').disabled===false")
    js("document.querySelector('#terminal-review').click()")
    wait("typeof releasePrepare==='function'")
    js("window.pwaHidden=true;Object.defineProperty(document,'hidden',{configurable:true,get:()=>pwaHidden});document.dispatchEvent(new Event('visibilitychange'));pwaHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('#terminal-text')?.value==='Unsent draft'&&document.querySelector('#terminal-review').disabled===false")
    js("releasePrepare();new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
    assert js("document.querySelector('.reader-review')===null")
    assert js("document.querySelector('#terminal-text').value")=='Unsent draft'
    assert not any(op=='commit' for op,_ in state['calls'])


@pytest.mark.parametrize('theme',['dark','light'])
def test_standalone_sized_reader_respects_safe_areas(phone,tmp_path,theme):
    client,js,wait,_=phone
    client.call('Emulation.setSafeAreaInsetsOverride',insets=dict(top=24,bottom=24,left=8,right=8))
    if theme=='light':
        js((ROOT/'dashboard/theme_core.js').read_text())
        js("document.documentElement.dataset.colorTheme='light';Object.entries(AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables).forEach(([k,v])=>document.documentElement.style.setProperty(k,v))")
    js("document.querySelector('#terminal-text').value='Continue the review';document.querySelector('#terminal-text').dispatchEvent(new Event('input'))")
    js("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
    value=js("""(()=>{const bar=document.querySelector('.reader-bar'),input=document.querySelector('.reader-input-bar'),r=input.getBoundingClientRect();return {
      width:innerWidth,height:innerHeight,top:parseFloat(getComputedStyle(bar).paddingTop),
      buttons:[...bar.querySelectorAll('button')].map(b=>b.getBoundingClientRect().top),
      bottom:r.bottom,left:r.left,right:r.right,field:document.querySelector('#terminal-text').getBoundingClientRect().height};})()""")
    assert value['width']==390 and value['height']==844,value
    assert value['top']>=24 and min(value['buttons'])>=24,value
    assert value['bottom']<=820 and value['left']>=8 and value['right']<=382 and value['field']>=44,value
    folder=Path(os.environ.get('MOBILE_PWA_CAPTURE_DIR',str(tmp_path)))
    folder.mkdir(parents=True,exist_ok=True)
    (folder/(theme+'.png')).write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png',captureBeyondViewport=False)['data']))
