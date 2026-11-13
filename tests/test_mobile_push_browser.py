"""Real Chrome/SW/permission and real local store; only Push service is fake."""
import base64
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import mobile_reader_browser as browser
from test_mobile_push import env  # Local cryptographic receiver and private temp store.

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


def test_notification_link_only_opens_an_agent_from_fresh_roster(phone_push):
    client,js,wait,click,env,calls=phone_push
    js("window.opened=[];OrreryMobileTerminal.open=n=>opened.push(n);location.hash='notify=SampleAgent';OrreryMobilePush.roster([{name:'SampleAgent'}])")
    assert js('opened')==['SampleAgent'] and js('location.hash')==''
    js("location.hash='notify=MissingAgent';OrreryMobilePush.roster([{name:'SampleAgent'}])")
    assert js('opened')==['SampleAgent']
    assert js("document.querySelector('#graph-reader-status').textContent")=='That agent is no longer available.'
    assert js('pushPermissionCalls')==0 and not env.service.devices


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
