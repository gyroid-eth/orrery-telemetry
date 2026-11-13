"""Real Chrome/SW/permission and real local store; only Push service is fake."""
import base64
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

import mobile_reader_browser as browser
from test_mobile_push import env, DEMO_NAME_CASES  # Local receiver and private temp store.
from dashboard.mobile_push import demo_name

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def phone_push(monkeypatch,env):
    if not os.environ.get('ORRERY_CDP_URL'):pytest.skip('ORRERY_CDP_URL required')
    env.service.call('remove',{'subscription':env.sub})
    original=browser.ThreadingHTTPServer
    calls=[]
    def server(address,parent):
        class Handler(parent):
            def do_GET(self):
                if self.path=='/view/push':return self.send(env.service.capability())
                return super().do_GET()
            def do_POST(self):
                if self.path.startswith('/view/push/'):
                    value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    op=self.path.rsplit('/',1)[-1];calls.append(op)
                    return self.send(env.service.call(op,value))
                return super().do_POST()
        return original(address,Handler)
    monkeypatch.setattr(browser,'ThreadingHTTPServer',server)
    fixture=browser.phone.__wrapped__(SimpleNamespace(param=(390,844)))
    client,js,wait,state=next(fixture)
    origin=js('location.origin')
    try:
        client.call('Browser.setPermission',permission={'name':'notifications'},setting='granted',origin=origin)
        # Real permission and SW registration; synthetic provider avoids any FCM
        # registration or publication. The real backend validates/stores it.
        js("window.testSubJSON="+json.dumps(env.sub))
        js("""(()=>{window.pushPermissionCalls=0;window.pushSubscribeCalls=0;window.pushUnsubscribed=0;
          const original=Notification.requestPermission.bind(Notification);
          Notification.requestPermission=(...args)=>{pushPermissionCalls++;return original(...args)};
          window.testPushSub=null;
          PushManager.prototype.getSubscription=async()=>window.testPushSub;
          PushManager.prototype.subscribe=async()=>{pushSubscribeCalls++;return window.testPushSub={toJSON:()=>window.testSubJSON,unsubscribe:async()=>{window.testPushSub=null;pushUnsubscribed++;return true}};};
        })()""")
        def click(selector):
            point=js("(()=>{const r=document.querySelector("+json.dumps(selector)+").getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
            client.call('Input.dispatchMouseEvent',type='mousePressed',button='left',clickCount=1,**point)
            client.call('Input.dispatchMouseEvent',type='mouseReleased',button='left',clickCount=1,**point)
        env.browser_state=state
        yield client,js,wait,click,env,calls
    finally:
        client.call('Browser.setPermission',permission={'name':'notifications'},setting='prompt',origin=origin)
        js("navigator.serviceWorker.getRegistration('/').then(r=>r?.unregister())")
        fixture.close()


def capture(client,name):
    directory=os.environ.get('MOBILE_PUSH_CAPTURE_DIR')
    if directory:
        target=Path(directory);target.mkdir(parents=True,exist_ok=True)
        (target/(name+'.png')).write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))


@pytest.mark.parametrize('theme',['dark','light'])
def test_reader_bell_permission_local_subscription_options_and_remove(phone_push,theme):
    client,js,wait,click,env,calls=phone_push
    js("document.documentElement.dataset.colorTheme="+json.dumps(theme))
    if theme=='light':
        js((ROOT/'dashboard/theme_core.js').read_text())
        js("Object.entries(AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables).forEach(([k,v])=>document.documentElement.style.setProperty(k,v))")
    assert js('pushPermissionCalls')==0
    click('.reader-push-bell');wait("document.querySelector('.push-enable')&&!document.querySelector('.push-enable').disabled")
    assert js('pushPermissionCalls')==0 and not env.service.devices
    capture(client,theme+'-before-enable')
    click('.push-enable');wait("document.querySelector('.push-enabled')!==null")
    assert js('pushPermissionCalls')==js('pushSubscribeCalls')==1 and len(env.service.devices)==1
    assert not js("document.querySelector('[data-setting=previews]').checked")
    click('[data-setting=finished]');wait("document.querySelector('.push-message').textContent==='Saved for this phone.'")
    assert js("document.querySelector('.reader-push-bell').getAttribute('aria-pressed')")=='true'
    click('[data-setting=previews]');wait("document.querySelector('[data-setting=previews]').checked&&!document.querySelector('[data-setting=previews]').disabled")
    assert next(iter(env.service.devices.values()))['previews'] is True
    capture(client,theme+'-enabled')
    click('.push-head button');wait("document.querySelector('.push-sheet')===null")
    capture(client,theme+'-bell-on')
    click('.reader-push-bell');wait("document.querySelector('.push-disable')&&!document.querySelector('.push-disable').disabled")
    click('.push-disable');wait("document.querySelector('.push-enable')!==null")
    assert not env.service.devices and js('pushUnsubscribed')==1
    assert calls.count('save')==3 and calls.count('remove')==1 and not env.state['calls']


@pytest.mark.parametrize('roster_problem',['missing','duplicate','retired','stale'])
def test_notification_waits_for_fresh_unique_nonretired_roster(roster_problem):
    fixture=browser.phone.__wrapped__(SimpleNamespace(param=(390,844)))
    client,js,wait,state=next(fixture)
    try:
        js('OrreryMobileTerminal.close(true);sessionStorage.clear()')
        original=state['snapshot']['agents'];source=state['snapshot']['sources']['agents']
        if roster_problem=='missing':state['snapshot']['agents']=[]
        elif roster_problem=='duplicate':state['snapshot']['agents']=original*2
        elif roster_problem=='retired':original[0]['retired']=True
        else:source['status']='stale'
        # Use the normal snapshot entry point, not a synthetic notification
        # dispatcher that bypasses Reader startup/identity validation.
        js("location.hash='notify=SampleAgent';document.querySelector('#time-window').dispatchEvent(new Event('input'))")
        wait("document.querySelector('#observed').textContent!=='Unknown'")
        # Poll completion is observed via its graph data, including stale source.
        wait("OrreryMobileIdentity.roster()===null" if roster_problem=='stale' else
             "document.querySelector('#graph-reader-status').textContent==='That agent is no longer available.'")
        assert not js("document.querySelector('#terminal-sheet').open")
        assert js('location.hash')=='#notify=SampleAgent'
        state['snapshot']['agents']=original;original[0]['retired']=False;source['status']='available'
        js("document.querySelector('#time-window').dispatchEvent(new Event('input'))")
        wait("document.querySelector('#terminal-sheet').open&&location.hash===''")
        js('history.back()');wait("!document.querySelector('#terminal-sheet').open")
        js('OrreryMobileTerminal.restore()');assert not js("document.querySelector('#terminal-sheet').open")
        assert js('location.hash')==''
    finally:fixture.close()


@pytest.mark.parametrize('delayed',['capabilities','roster'])
@pytest.mark.parametrize('theme',['dark','light'])
def test_notification_cold_open_waits_for_reader_and_roster(monkeypatch,delayed,theme):
    # Review proof: a fast roster used to consume the hash before capability
    # checks completed; also cover the reverse response order.
    original=browser.ThreadingHTTPServer;gate={'delay':False}
    def server(address,parent):
        class Handler(parent):
            def do_GET(self):
                paths={'/view/history','/view/terminal'} if delayed=='capabilities' else {'/view/snapshot'}
                if gate['delay'] and self.path.split('?')[0] in paths:time.sleep(1.5)
                return super().do_GET()
        return original(address,Handler)
    monkeypatch.setattr(browser,'ThreadingHTTPServer',server)
    fixture=browser.phone.__wrapped__(SimpleNamespace(param=(390,844)))
    client,js,wait,state=next(fixture)
    try:
        js('OrreryMobileTerminal.close(true);sessionStorage.clear()');gate['delay']=True
        client.call('Page.navigate',url=js('location.origin')+'/?cold=1#notify=SampleAgent')
        wait("typeof OrreryMobileTerminal!=='undefined'")
        assert js('location.hash')=='#notify=SampleAgent' and not js("document.querySelector('#terminal-sheet').open")
        wait("document.querySelector('#terminal-sheet').open&&location.hash===''")
        wait("document.querySelector('[data-id=question]')!==null")
        js('document.fonts.ready')
        js('document.documentElement.dataset.colorTheme='+json.dumps(theme))
        if theme=='light':
            js((ROOT/'dashboard/theme_core.js').read_text())
            js("Object.entries(AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables).forEach(([k,v])=>document.documentElement.style.setProperty(k,v))")
        capture(client,theme+'-cold-'+delayed+'-reader-open')
        assert sum(op=='targets' for op,_ in state['calls'])==2  # initial fixture + notification
    finally:fixture.close()


def test_notification_reload_discards_existing_input_draft():
    # Preserve the review's documented existing navigation behavior. The worker
    # navigates to a URL, so this does not persist a private draft in storage.
    fixture=browser.phone.__wrapped__(SimpleNamespace(param=(390,844)))
    client,js,wait,state=next(fixture)
    try:
        js("document.querySelector('.reader-input-bar button').click()")
        wait("document.querySelector('#terminal-text')!==null&&!document.querySelector('#terminal-text').disabled")
        js("(()=>{const e=document.querySelector('#terminal-text');e.value='Unsaved review draft';e.dispatchEvent(new Event('input',{bubbles:true}));})()")
        assert js("document.querySelector('#terminal-text').value")=='Unsaved review draft'
        client.call('Page.navigate',url=js('location.origin')+'/#notify=SampleAgent')
        wait("document.querySelector('#terminal-sheet').open&&location.hash===''",timeout=12)
        assert not js("document.body.textContent.includes('Unsaved review draft')")
        assert js("document.querySelector('#terminal-text')?.value||''")!='Unsaved review draft'
    finally:fixture.close()


def test_notification_dispatch_passes_fresh_mail_identity(monkeypatch):
    original=browser.ThreadingHTTPServer
    def server(address,parent):
        class Handler(parent):
            def send(self,data,*args,**kwargs):
                if isinstance(data,dict) and data.get('api')==31:
                    data={**data,'api':34,'supported':'fresh-managed-pane'}
                return super().send(data,*args,**kwargs)
        return original(address,Handler)
    monkeypatch.setattr(browser,'ThreadingHTTPServer',server)
    fixture=browser.phone.__wrapped__(SimpleNamespace(param=(390,844)))
    client,js,wait,state=next(fixture)
    try:
        identity=dict(mail_instance_id='a'*32,mail_agent_id=1)
        state['snapshot']['agents'][0].update(identity)
        state['targets']=[dict(choice='fixture-choice',name='SampleAgent',pane_label='Window 0 · pane 0',revision='fixture-revision',**identity)]
        state['calls'].clear();js('OrreryMobileTerminal.close(true);sessionStorage.clear()')
        js("location.hash='notify=SampleAgent';document.querySelector('#time-window').dispatchEvent(new Event('input'))")
        wait("document.querySelector('#terminal-sheet').open&&location.hash===''")
        wait("document.querySelector('.reader-input-bar button')!==null")
        targets=[p for op,p in state['calls'] if op=='targets']
        assert len(targets)==1 and all(targets[0][k]==v for k,v in identity.items())
        assert not js("document.querySelector('#terminal-sheet').textContent.includes('A unique Mail identity is required')")
        capture(client,'fresh-notification-reader-open')
    finally:fixture.close()


def test_service_worker_only_notifies_and_clicks_same_origin(tmp_path):
    # Exercise the actual worker handlers without publishing or OS delivery.
    script="""const fs=require('fs'),vm=require('vm'),assert=require('assert');
    const handlers={},shown=[],opened=[],navigation=[];
    const self={location:{origin:'https://fixture.example.ts.net:9443'},addEventListener:(n,h)=>handlers[n]=h,
      registration:{showNotification:async(t,o)=>shown.push([t,o])},clients:{matchAll:async()=>[],openWindow:async u=>opened.push(u)}};
    vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),{self,URL});
    (async()=>{let pending;handlers.push({data:{json:()=>({title:'Agent — Reply needed',body:'Question',tag:'a'.repeat(32),agent:'SampleAgent',url:'https://evil.test'})},waitUntil:p=>pending=p});await pending;
      assert.equal(shown.length,1);handlers.notificationclick({notification:{data:shown[0][1].data,close(){}},waitUntil:p=>pending=p});await pending;
      assert.equal(opened[0],'https://fixture.example.ts.net:9443/#notify=SampleAgent');
      self.clients.matchAll=async()=>[{url:'https://fixture.example.ts.net:9443/',navigate:async u=>navigation.push(u),focus:async()=>{}}];
      handlers.notificationclick({notification:{data:{agent:'SampleAgent'},close(){}},waitUntil:p=>pending=p});await pending;
      assert.equal(navigation.length,1);assert(!handlers.fetch);
      handlers.notificationclick({notification:{data:{agent:'https://evil.test'},close(){}},waitUntil:p=>pending=p});
      assert.equal(opened.length,1);
      handlers.push({data:{json:()=>({title:'Agent',body:'😀'.repeat(600),tag:'b'.repeat(32),agent:'SampleAgent'})},waitUntil:p=>pending=p});await pending;
      assert.equal(shown.length,2);console.log('worker delivery and same-origin click PASS');
    })().catch(e=>{console.error(e);process.exit(1)});
    """
    path=tmp_path/'worker-check.cjs';path.write_text(script)
    done=subprocess.run(['node',str(path),str(ROOT/'dashboard/mobile/push-sw.js')],capture_output=True,text=True)
    assert done.returncode==0,done.stderr


def set_demo(env,js,wait,on,words=None):
    if not os.environ.get('ORRERY_DEMO_BRIDGE_ROOT'):
        pytest.skip('paired desktop scripts required')
    env.browser_state['demo']=dict(version=1,supported=True,on=on,words=(words or ['Orchid']) if on else [],
                                   identity=dict(users=['viewer'],hosts=['viewer-host']) if on else None)
    env.state['demo']=on
    js('OrreryMobileDemo.refresh()')
    wait("document.documentElement.dataset.demoSync==='ready'&&window.OrreryDemo?.on==="+str(on).lower())


@pytest.mark.parametrize('theme',['dark','light'])
def test_demo_enable_and_other_settings_preserve_saved_preview(phone_push,theme):
    client,js,wait,click,env,calls=phone_push
    if theme=='light':
        js((ROOT/'dashboard/theme_core.js').read_text())
        js("document.documentElement.dataset.colorTheme='light';Object.entries(AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables).forEach(([k,v])=>document.documentElement.style.setProperty(k,v))")
    set_demo(env,js,wait,True)
    assert js("document.querySelector('#terminal-title').textContent")=='SampleAgent'
    click('.reader-push-bell');wait("document.querySelector('.push-enable:not([disabled])')!==null")
    assert js('pushPermissionCalls')==0
    assert not js("document.querySelector('.push-sheet').textContent.includes('paused in Demo')")
    click('.push-enable');wait("document.querySelector('.push-enabled')!==null")
    assert js('pushPermissionCalls')==js('pushSubscribeCalls')==1
    click('[data-setting=reply_needed]');wait("document.querySelector('.push-message').textContent==='Saved for this phone.'")
    click('[data-setting=finished]');wait("document.querySelector('.reader-push-bell').getAttribute('aria-pressed')==='true'")
    assert js("document.querySelector('[data-setting=previews]').disabled&&!document.querySelector('[data-setting=previews]').checked")
    assert js("document.querySelector('[data-setting=previews]').closest('label').querySelector('small').textContent")=='Previews are hidden in Demo.'
    assert next(iter(env.service.devices.values()))['reply_needed'] is False
    capture(client,theme+'-demo-enabled')
    saves=calls.count('save');set_demo(env,js,wait,False)
    wait("!document.querySelector('[data-setting=previews]').disabled")
    assert calls.count('save')==saves
    click('[data-setting=previews]');wait("document.querySelector('[data-setting=previews]').checked&&!document.querySelector('[data-setting=previews]').disabled")
    assert next(iter(env.service.devices.values()))['previews'] is True
    saves=calls.count('save');set_demo(env,js,wait,True)
    wait("document.querySelector('[data-setting=previews]').disabled&&!document.querySelector('[data-setting=previews]').checked")
    assert calls.count('save')==saves and next(iter(env.service.devices.values()))['previews'] is True
    capture(client,theme+'-demo-preview-hidden')
    click('[data-setting=reply_needed]');wait("document.querySelector('.push-message').textContent==='Saved for this phone.'")
    assert next(iter(env.service.devices.values()))['previews'] is True
    set_demo(env,js,wait,False)
    wait("document.querySelector('[data-setting=previews]').checked&&!document.querySelector('[data-setting=previews]').disabled")
    assert not env.state['calls']


def test_demo_notification_name_matches_phone_mask_and_keeps_raw_route(phone_push):
    client,js,wait,click,env,calls=phone_push
    set_demo(env,js,wait,True,['SampleAgent'])
    wait("document.querySelector('#terminal-title').textContent==='***********'")
    click('.reader-push-bell');wait("document.querySelector('.push-agent').textContent==='***********'")
    wait("document.querySelector('.push-enable:not([disabled])')!==null")
    click('.push-enable');wait("document.querySelector('.push-enabled')!==null")
    device=next(iter(env.service.devices.values()))
    click('[data-setting=finished]');wait("document.querySelector('.reader-push-bell').getAttribute('aria-pressed')==='true'")
    assert list(device['agents'].values())==['SampleAgent']
    for name,settings,expected in DEMO_NAME_CASES:
        identity={**settings.get('identity',{}),'words':settings.get('words',[])}
        shown=js('OrreryDemoMask.create('+json.dumps(identity)+').mask('+json.dumps(name)+')')
        assert shown==expected==demo_name(name,dict(on=True,**settings))
    capture(client,'demo-name-masked')
    assert not env.state['calls']
