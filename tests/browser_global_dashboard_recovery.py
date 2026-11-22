"""Optional real-browser regression with synthetic HTTP data and own CDP tabs.

Run through an isolated HOME/TMUX runner. --cdp-helper points to the existing
browser bridge; this script never launches a browser or a provider binary.
"""
import sys,pathlib,json,threading,base64,time,hashlib,argparse
from http.server import ThreadingHTTPServer
parser=argparse.ArgumentParser();parser.add_argument('--output',type=pathlib.Path,required=True);parser.add_argument('--cdp-helper',type=pathlib.Path,required=True);options=parser.parse_args()
ROOT=pathlib.Path(__file__).resolve().parents[1];QA=options.output;QA.mkdir(parents=True,exist_ok=True)
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'packages/agentstack_mail/tests'),str(ROOT)]
import pytest
from service_teardown import TEST_LABEL_PREFIX
from test_global_child_client import server
from dashboard import server as dashboard
from _dashboard_http_fixture import install_offline_quotas, dashboard_http_fixture
from dashboard.http_operator import configure,read_capability
from dashboard.global_backend import OPERATOR_API
from agentstack_mail.global_s2c import S2cRuntime
from test_global_server_s2b import owner
import uuid
cdp={};exec(options.cdp_helper.read_text().split('\ndef main():')[0],cdp)
result={};tabs=[]
with pytest.MonkeyPatch.context() as mp:
 gen=server.__wrapped__(mp);s=next(gen);http=None
 try:
  mp.setenv('AGENTSTACK_MCP_URL',s['url']);mp.setenv('AGENTSTACK_GLOBAL_SERVER_CONFIG',str(s['config_path']))
  home=s['root']/'agentstack';configure(home);mp.setenv('AGENTSTACK_HOME',str(home));mp.setenv('AGENTSTACK_LABEL_PREFIX',TEST_LABEL_PREFIX);token=read_capability(home)
  mp.setattr(dashboard,'GLOBAL_MODE',True);mp.setattr(dashboard,'BIND_HOST','127.0.0.1')
  runtime=S2cRuntime(s['config_path'])
  def send(subject,to,reply=None):
   args=owner(s,1,request_id=str(uuid.uuid4()),to_agent_ids=to,subject=subject,body_md=subject+' body <script>window.injected=true</script>\n'+'Long fixture text. '*30)
   if reply is not None:args['reply_to']=reply
   return runtime.apply('send_message',args)['message_id']
  hidden=send('HIDDEN_SENTINEL',[1]);vis=send('Visible root',[2],hidden)
  for i in range(6):vis=send('Visible reply '+str(i),[2],vis)
  op=OPERATOR_API['RuntimeOperator'](s['config_path'])
  op.prepare('pending');c=op.client('pending');C=type(c);original=C.rpc
  def unsent(self,method,params):
   if self is c and params.get('name')=='register_agent':raise op.api['ClientError']('TRANSPORT_FAILED')
   return original(self,method,params)
  C.rpc=unsent
  try:
   try:c.create('RecoverUnsent','daemon','deterministic')
   except RuntimeError as e:assert str(e)=='TRANSPORT_FAILED'
  finally:C.rpc=original
  pending=c.outputs['registration'];pending_before=pending.read_bytes()
  # Existing fixed client records only; no real provider or process is launched.
  from test_global_runtime_client_s2a import context
  observed_client=context(s,name='observed',window=False)
  child_row=observed_client.prepare_child(dict(child_client_name='observed-child',name='ObservedChild',program='codex',model='fixture',prepare_only=True))
  observed_child=type(observed_client)(observed_client.clients_parent/'observed-child/runtime-client.json')
  for c,pid in ((observed_client,101),(observed_child,102)):
   directory=c.runtime_dir/'live-sessions';directory.mkdir(mode=0o700)
   op.api['atomic_json'](directory/'fixture.json',dict(kind='orrery-global-live-session-v1',**c.binding,agent_id=c.identity['agent_id'],session_id='fixture',pid=pid,process_start='fixture',pane_id='%10'))
   with c.operation_mutex():pass
  mp.setitem(__import__('dashboard.global_backend',fromlist=['OPERATOR_API']).OPERATOR_API['API'],'pane_evidence',lambda live,**kw:('live','%10','ctx: 42% used\n✻ Working…' if live['pid']==101 else 'ctx: 0% used\nReady'))
  install_offline_quotas(dashboard,monkeypatch=mp)
  http=dashboard_http_fixture(dashboard)
  thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
  op.create('trust-needed',agent_name='TrustNeedsApproval',program='codex',model='fixture')
  origin=f'http://127.0.0.1:{http.server_port}'
  def tab(pre='',query='?view_as_agent_id=2'):
   t=cdp['http']('/json/new?about:blank',method='PUT');tabs.append(t['id']);w=cdp['WS'](t['webSocketDebuggerUrl']);w.sock.settimeout(20)
   w.call('Page.enable');w.call('Runtime.enable');w.call('Emulation.setDeviceMetricsOverride',width=1400,height=1000,deviceScaleFactor=1,mobile=False)
   injection="window.__errors=[];window.addEventListener('unhandledrejection',e=>window.__errors.push(String(e.reason)));window.addEventListener('error',e=>window.__errors.push(e.message));"+pre
   w.call('Page.addScriptToEvaluateOnNewDocument',source=injection);w.call('Page.navigate',url=origin+'/'+query);return w
  def ev(w,js):
   r=w.call('Runtime.evaluate',expression=js,awaitPromise=True,returnByValue=True)
   if 'exceptionDetails' in r:raise RuntimeError(str(r['exceptionDetails']))
   return r.get('result',{}).get('value')
  def wait(w,expression):
   deadline=time.monotonic()+15
   while time.monotonic()<deadline:
    if ev(w,expression):return
    time.sleep(.1)
   raise RuntimeError('browser wait timed out: '+expression+' '+str(ev(w,"({errors:window.__errors,status:document.querySelector('.global-status')?.textContent,nodes:[...document.querySelectorAll('#gsvg .node')].map(n=>[n.getAttribute('class'),n.getAttribute('aria-label')]),text:document.body.innerText.slice(-600)})")))
  def shot(w,name):
   ev(w,'document.fonts.ready');time.sleep(.3)
   data=w.call('Page.captureScreenshot',format='png')['data'];(QA/name).write_bytes(base64.b64decode(data))
  init="sessionStorage.setItem('orrery.operator-capability',"+json.dumps(token)+");window.__requests=[];window.__aborts=0;const realFetch=window.fetch.bind(window);window.fetch=async function(path,options){window.__requests.push({path:String(path),method:options?.method||'GET'});options?.signal?.addEventListener('abort',()=>window.__aborts++);const r=await realFetch(path,options);if(window.__holdRoster&&(window.__holdPath?String(path).startsWith(window.__holdPath):String(path).startsWith('/api/agents'))){window.__holdRoster=false;return {ok:r.ok,status:r.status,json:async()=>{const j=await r.json();await new Promise(resolve=>window.__releaseRoster=resolve);return j;}};}return r;};"
  # Supplementary observation failure must not revoke the operator session.
  failed_observation=tab("window.__observationStatus=503;const realFetch=window.fetch.bind(window);window.fetch=(path,options)=>String(path).startsWith('/api/runtime-observations')&&window.__observationStatus?Promise.resolve(new Response(JSON.stringify({ok:false,reason:'OBSERVATION_FIXTURE_UNAVAILABLE'}),{status:window.__observationStatus,headers:{'Content-Type':'application/json'}})):realFetch(path,options);",query='?view=net&view_as_agent_id=2&message_id='+str(vis))
  wait(failed_observation,"!!document.querySelector('[name=capability]')")
  ev(failed_observation,"document.querySelector('[name=capability]').value="+json.dumps(token)+";document.querySelector('.global-form').requestSubmit()")
  wait(failed_observation,"!document.querySelector('[name=capability]') && document.querySelectorAll('.global-agent').length>0 && document.querySelectorAll('.reply-item').length===7")
  ev(failed_observation,"document.querySelector('#v-net').click()")
  wait(failed_observation,"document.querySelectorAll('#gsvg .node').length>0")
  result['observation_503']=ev(failed_observation,"({stored:!!sessionStorage.getItem('orrery.operator-capability'),mail:document.querySelectorAll('.reply-item').length,allUnknown:[...document.querySelectorAll('#gsvg .node')].every(n=>n.classList.contains('unknown')||n.classList.contains('retired'))})")
  assert result['observation_503']==dict(stored=True,mail=7,allUnknown=True)
  shot(failed_observation,'observation-unavailable.png')
  ev(failed_observation,"window.__observationStatus=0;window.OrreryGlobalDashboard.refresh()")
  wait(failed_observation,"!!document.querySelector('#gsvg .node.act-work')")
  ev(failed_observation,"window.__observationStatus=503;window.OrreryGlobalDashboard.refresh()")
  result['observation_refresh_discards_previous']=ev(failed_observation,"document.querySelectorAll('#gsvg .act-work,#gsvg .act-wait').length===0 && document.querySelectorAll('.global-agent').length>0 && !document.querySelector('[name=capability]') && !!sessionStorage.getItem('orrery.operator-capability')")
  assert result['observation_refresh_discards_previous']
  ev(failed_observation,"document.querySelector('#newbtn').click()")
  assert ev(failed_observation,"!!document.querySelector('.global-form [name=client_name]')")
  ev(failed_observation,"[...document.querySelectorAll('.global-form button')].find(x=>x.textContent==='Cancel').click();window.__observationStatus=403;window.OrreryGlobalDashboard.refresh().catch(()=>{})")
  wait(failed_observation,"!!document.querySelector('[name=capability]') && document.querySelectorAll('.global-agent,.reply-item,#gsvg .node').length===0")
  result['observation_403_locks']=ev(failed_observation,"!sessionStorage.getItem('orrery.operator-capability')")
  assert result['observation_403_locks']
  no_viewer=tab(init,query='?view=net')
  wait(no_viewer,"!!document.querySelector('#gsvg .node.act-work.sp-parent') && !!document.querySelector('#gsvg .node.act-wait.sp-child')")
  result['runtime_without_viewpoint']=ev(no_viewer,"({viewer:document.querySelector('select[aria-label=\"Mail viewpoint\"]').value,work:document.querySelectorAll('#gsvg .act-work').length,wait:document.querySelectorAll('#gsvg .act-wait').length,mailReads:window.__requests.filter(x=>x.path.startsWith('/api/graph')||x.path.startsWith('/api/mail/')).length})")
  assert result['runtime_without_viewpoint']==dict(viewer='',work=1,wait=1,mailReads=0)
  shot(no_viewer,'runtime-without-viewpoint.png')
  ev(no_viewer,"document.querySelector('#v-deck').click()")
  result['deck_runtime']=ev(no_viewer,"[...document.querySelectorAll('.global-agent')].map(n=>n.textContent)")
  assert any('WORK' in x and 'Beta' in x for x in result['deck_runtime'])
  assert any('WAIT' in x and 'ObservedChild' in x for x in result['deck_runtime'])
  assert all('session activity unavailable' not in x for x in result['deck_runtime'])
  shot(no_viewer,'deck-runtime.png')
  w=tab(init,query='?view_as_agent_id=2&message_id='+str(vis));wait(w,"!!window.OrreryGlobalDashboard && document.querySelectorAll('.global-agent').length>0")
  wait(w,"document.querySelectorAll('.reply-item').length===7")
  result['visible_mail']=ev(w,"({cards:document.querySelectorAll('.reply-item').length,hiddenInDOM:document.documentElement.outerHTML.includes('HIDDEN_SENTINEL'),hiddenInURL:location.href.includes('HIDDEN_SENTINEL'),injected:!!window.injected,header:[...document.querySelectorAll('#edrawer .ed-a,#edrawer .ed-b')].map(x=>x.textContent),errors:window.__errors})")
  assert not result['visible_mail']['hiddenInDOM'] and not result['visible_mail']['injected'] and not result['visible_mail']['errors']
  assert ev(w,"getComputedStyle(document.body).backgroundColor!=='rgba(0, 0, 0, 0)' && getComputedStyle(document.querySelector('header')).display==='flex'")
  ev(w,"document.querySelector('#v-net').click()")
  wait(w,"!!document.querySelector('#gsvg .node.act-work.sp-parent') && !!document.querySelector('#gsvg .node.act-wait.sp-child')")
  result['runtime_projection']=ev(w,"({work:document.querySelectorAll('#gsvg .act-work').length,wait:document.querySelectorAll('#gsvg .act-wait').length,unknown:document.querySelectorAll('#gsvg .unknown').length,ctx42:!!document.querySelector('#gsvg .act-work .ctx-arc[data-remaining=\"58\"]'),ctx0:!!document.querySelector('#gsvg .act-wait .ctx-arc[data-remaining=\"100\"]')})")
  assert result['runtime_projection']['work']==1 and result['runtime_projection']['wait']==1 and result['runtime_projection']['ctx42'] and result['runtime_projection']['ctx0']
  shot(w,'runtime-network-dark.png');ev(w,"window.AgentStackColorTheme.setPreference('light')");shot(w,'runtime-network-light.png');ev(w,"window.AgentStackColorTheme.setPreference('dark');document.querySelector('#v-deck').click()")
  shot(w,'global-mail-dark.png');ev(w,"window.AgentStackColorTheme.setPreference('light')");shot(w,'global-mail-light.png')
  w.call('Emulation.setDeviceMetricsOverride',width=316,height=900,deviceScaleFactor=1,mobile=False);shot(w,'global-mail-narrow.png')
  result['narrow']=ev(w,"({width:innerWidth,body:document.body.scrollWidth,drawer:document.querySelector('#edrawer').getBoundingClientRect().width})")
  w.call('Emulation.setDeviceMetricsOverride',width=1400,height=1000,deviceScaleFactor=1,mobile=False)
  ev(w,"document.querySelector('#newbtn').click();for(const [n,v] of Object.entries({client_name:'pending',name:'RecoverUnsent',program:'daemon',model:'deterministic',cwd:"+json.dumps(str(s['root']/'home'))+"})){document.querySelector('.global-form').elements[n].value=v;}document.querySelector('.global-form').requestSubmit();")
  wait(w,"[...document.querySelectorAll('.global-form button')].some(x=>x.textContent==='replay-registration')")
  wait(w,"document.querySelector('.global-form').textContent.includes('registration_pending')")
  result['pending_recovery']=ev(w,"({text:document.querySelector('.global-form').textContent,requests:window.__requests.filter(x=>x.path.includes('/api/global/'))})")
  result['pending_recovery']['pending_bytes_unchanged']=pending.read_bytes()==pending_before
  result['pending_recovery']['server_agent_id']=op.read({'kind':'resolve_name','name':'RecoverUnsent'})['data']['agent_id']
  assert result['pending_recovery']['server_agent_id'] is None
  assert result['pending_recovery']['pending_bytes_unchanged']
  ev(w,"[...document.querySelectorAll('.global-form button')].find(x=>x.textContent==='replay-registration').click()")
  wait(w,"document.querySelector('.global-form').textContent.includes('registered_unconfigured')")
  recovered=op.client('pending')
  result['pending_recovery']['requests']=ev(w,"window.__requests.filter(x=>x.path.includes('/api/global/'))")
  result['pending_recovery']['replayed']=recovered.identity['agent_id']==op.read({'kind':'resolve_name','name':'RecoverUnsent'})['data']['agent_id']
  assert recovered.local_owner()==json.loads(pending_before)['registration_token']
  assert result['pending_recovery']['replayed'] and not pending.exists()
  shot(w,'pending-recovery.png')
  result['persistent_guidance']=ev(w,"({reason:document.querySelector('.global-form').textContent.includes('PERSISTENT_EXECUTION_REQUIRED'),noConfigure:![...document.querySelectorAll('.global-form button')].some(x=>x.textContent==='configure-provider'),cli:document.querySelector('.global-form').textContent.includes('agentstack-persistent configure --operator'),links:[...document.querySelectorAll('.global-form a')].map(x=>x.getAttribute('href'))})")
  assert all(result['persistent_guidance'][k] for k in ('reason','noConfigure','cli')),result['persistent_guidance']
  assert len(result['persistent_guidance']['links'])==2 and all(x.endswith('#persistent-execution') for x in result['persistent_guidance']['links'])
  ev(w,"[...document.querySelectorAll('.global-form button')].find(x=>x.textContent==='Cancel').click();document.querySelector('#newbtn').click();for(const [n,v] of Object.entries({client_name:'trust-needed',name:'TrustNeedsApproval',program:'codex',model:'fixture',cwd:"+json.dumps(str(s['root']/'home'))+"})){document.querySelector('.global-form').elements[n].value=v;}document.querySelector('.global-form').requestSubmit();")
  wait(w,"document.querySelector('.global-form').textContent.includes('INSTRUCTION_PROVIDER_CONFIG_REQUIRED')")
  assert ev(w,"[...document.querySelectorAll('.global-form button')].some(x=>x.textContent==='configure-provider')")
  ev(w,"[...document.querySelectorAll('.global-form button')].find(x=>x.textContent==='configure-provider').click()")
  wait(w,"document.querySelector('.global-form').textContent.includes('CODEX_HOOK_CONFIGURE_REQUIRED')")
  result['trust_guidance']=ev(w,"({noConfigure:![...document.querySelectorAll('.global-form button')].some(x=>x.textContent==='configure-provider'),cli:document.querySelector('.global-form').textContent.includes('codex-plan')&&document.querySelector('.global-form').textContent.includes('codex-apply'),links:[...document.querySelectorAll('.global-form a')].map(x=>x.getAttribute('href'))})")
  assert result['trust_guidance']['noConfigure'] and result['trust_guidance']['cli'],result['trust_guidance']
  assert len(result['trust_guidance']['links'])==2 and all(x.endswith('#codex-hook-trust') for x in result['trust_guidance']['links'])
  trust_client=op.client('trust-needed')
  assert not (trust_client.runtime_dir/'provider-home/codex/hook-plan.json').exists()
  assert not trust_client.outputs['launch'].exists()
  shot(w,'operator-configuration-guidance.png')
  ev(w,"[...document.querySelectorAll('.global-form button')].find(x=>x.textContent==='Cancel').click();window.__holdRoster=true;window.__refresh=window.OrreryGlobalDashboard.refresh().catch(e=>String(e));void 0;")
  wait(w,"typeof window.__releaseRoster==='function'")
  ev(w,"window.OrreryGlobalDashboard.lock();window.__releaseRoster();")
  ev(w,"window.__refresh")
  result['lock_during_body']=ev(w,"({stored:!!sessionStorage.getItem('orrery.operator-capability'),cards:document.querySelectorAll('.global-agent').length,selectors:document.querySelector('select[aria-label=\"Mail viewpoint\"]').options.length,lockedDialog:!!document.querySelector('[name=capability]'),netinfo:document.querySelector('#netinfo').textContent})")
  assert result['lock_during_body']['cards']==0 and result['lock_during_body']['selectors']==0
  assert ev(w,'window.__aborts>0')
  result['locked_drawer_metadata']=ev(w,"({a:document.querySelector('#edrawer .ed-a').textContent,b:document.querySelector('#edrawer .ed-b').textContent,count:document.querySelector('#edrawer .ed-cnt').textContent,aria:document.querySelector('#edrawer').getAttribute('aria-hidden'),open:document.querySelector('#edrawer').classList.contains('on')})")
  assert result['locked_drawer_metadata']=={'a':'—','b':'—','count':'0','aria':'true','open':False},result['locked_drawer_metadata']
  shot(w,'locked-after-delayed-body.png')
  result['delayed_reads']={}
  for name,path,query in [('health','/api/mail-watcher-health',''),('detail','/api/mail/message','?view_as_agent_id=2&message_id='+str(vis)),('validation','/api/version','')]:
   ev(w,"window.__holdPath="+json.dumps(path)+";window.__holdRoster=true;document.querySelector('[name=capability]').value="+json.dumps(token)+";document.querySelector('.global-form').requestSubmit()")
   wait(w,"window.__holdRoster===false && typeof window.__releaseRoster==='function'")
   # detail is requested by opening a saved conversation, not by roster refresh.
   ev(w,"window.OrreryGlobalDashboard.lock();window.__releaseRoster();")
   time.sleep(.2)
   result['delayed_reads'][name]=ev(w,"document.querySelectorAll('.global-agent,.reply-item,#gsvg .node').length===0 && document.querySelector('[name=capability]')?.value===''")
   assert result['delayed_reads'][name]
   ev(w,"window.__holdPath=null;window.__releaseRoster=null")
  # An unsolicited old share after Lock must not unlock a listener.
  ev(w,"window.__lateChannel=new BroadcastChannel('orrery.operator-session');window.__lateChannel.postMessage({type:'share',capability:"+json.dumps(token)+"});window.OrreryGlobalDashboard.lock();")
  time.sleep(.7)
  result['queued_share_before_lock']=ev(w,"({stored:!!sessionStorage.getItem('orrery.operator-capability'),lockedDialog:!!document.querySelector('[name=capability]'),cards:document.querySelectorAll('.global-agent').length})")
  assert not result['queued_share_before_lock']['stored'] and result['queued_share_before_lock']['lockedDialog']
  ev(w,"window.__lateChannel.close();window.OrreryGlobalDashboard.lock()")
  # Storage exception occurs before initialization; record rather than hide it.
  w2=tab("for(const name of ['getItem','setItem','removeItem'])Storage.prototype[name]=function(){throw new DOMException('denied','SecurityError')};",query='')
  time.sleep(1)
  result['storage_unavailable']=ev(w2,"({initialized:!!window.OrreryGlobalDashboard,unlockInput:!!document.querySelector('[name=capability]'),errors:window.__errors})")
  assert result['storage_unavailable']['initialized'] and result['storage_unavailable']['unlockInput']
  ev(w2,"document.querySelector('[name=capability]').value="+json.dumps(token)+";document.querySelector('.global-form').requestSubmit()")
  wait(w2,"document.querySelectorAll('.global-agent').length>0 && !document.querySelector('[name=capability]')")
  result['storage_unavailable']['memory_unlock']=ev(w2,"document.querySelector('.global-status').textContent.includes('BROWSER_SESSION_STORAGE_UNAVAILABLE')")
  assert result['storage_unavailable']['memory_unlock'] and not ev(w2,"window.__errors.length")
  w2.call('Page.reload');wait(w2,"!!document.querySelector('[name=capability]')");result['storage_unavailable']['reload_requires_unlock']=True
  ev(w2,"window.OrreryGlobalDashboard.lock()");time.sleep(.2)
  # Unrequested share is ignored, while a fresh tab can request a matching share.
  ev(w,"document.querySelector('[name=capability]').value="+json.dumps(token)+";document.querySelector('.global-form').requestSubmit()")
  wait(w,"!document.querySelector('[name=capability]')")
  peer=tab(query='');wait(peer,"document.querySelectorAll('.global-agent').length>0")
  result['correlated_share']=True
  ev(w,"window.OrreryGlobalDashboard.lock()")
  wait(peer,"!!document.querySelector('[name=capability]') && document.querySelectorAll('.global-agent').length===0")
  result['peer_lock']=True
  # The shared session is also the sole authenticated binary fetch path.
  result['binary_session']=ev(w,"(async()=>{let revoked=[];const oldRevoke=URL.revokeObjectURL;URL.revokeObjectURL=u=>{revoked.push(u);oldRevoke.call(URL,u)};const s=window.OrreryOperatorSession.create();await s.accept("+json.dumps(token)+");let pathRejected=false;try{await s.api('/\\\\attacker.example/secret')}catch(e){pathRejected=e.message==='HTTP_OPERATOR_PATH_INVALID'}const url=await s.apiBlob('/portrait?name=Fixture');const text=await (await fetch(url)).text();s.lock();URL.revokeObjectURL=oldRevoke;return {pathRejected,svg:text.includes('<svg'),revoked:revoked.includes(url),unlocked:s.unlocked};})()")
  assert result['binary_session']==dict(pathRejected=True,svg=True,revoked=True,unlocked=False)
  result['head']=__import__('subprocess').check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()

 finally:
  for tid in tabs:
   try:cdp['http']('/json/close/'+tid)
   except Exception:pass
  if http:http.shutdown();http.server_close();thread.join(timeout=5)
  gen.close()
(QA/'browser-result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
