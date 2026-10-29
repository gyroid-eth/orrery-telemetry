/* View-only reader: metadata lists, explicit single-message disclosure, no offline bodies. */
(function(){
'use strict';
window.AGENTSTACK_DSH_ENABLED=true;
window.AGENTSTACK_DEMO={assetURL:n=>'/assets/'+n+'.svg',portraitURL:n=>'/view/portrait?window='+desiredWindow+'&name='+n};
const $=s=>document.querySelector(s),node=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;};
let snapshot=null,kind='all',agentFilter='',desiredWindow='day',timer=0,active=null,pending=false,receivedAt=0,lastNotices=[];
const mailRef=a=>a&&/^[a-f0-9]{32}$/.test(a.mail_instance_id)&&Number.isSafeInteger(a.mail_agent_id)&&a.mail_agent_id>0?{mail_instance_id:a.mail_instance_id,mail_agent_id:a.mail_agent_id}:null;
function markRef(element,value){const ref=mailRef(value);if(ref){element.dataset.mailInstanceId=ref.mail_instance_id;element.dataset.mailAgentId=String(ref.mail_agent_id);}}
window.OrreryMobileIdentity={forName(name){const rows=(snapshot?.agents||[]).filter(a=>a.name===name);return rows.length===1?mailRef(rows[0]):null;}};
let mailPage=null,mailPositions=[null],mailPosition=0,mailController=null,mailGeneration=0;
let pair=null,pairController=null,pairGeneration=0,detailController=null,detailGeneration=0;
const minutesAt=position=>Math.max(15,Math.min(43200,Math.round(Math.exp(Math.log(15)+position/1000*Math.log(43200/15))/15)*15));
const duration=seconds=>seconds===null?'All history':seconds<3600?'Last '+Math.round(seconds/60)+' min':seconds<=86400?'Last '+(seconds/3600).toLocaleString('en-GB',{maximumFractionDigits:2})+'h':'Last '+(seconds/86400).toLocaleString('en-GB',{maximumFractionDigits:2})+'d';
window.OrreryMobileWindow={duration};
const network=new window.OrreryMobileNetwork(null,(a,b)=>openPair(a,b));
let graphFullscreen=null;
const time=ts=>typeof ts==='number'?new Date(ts*1000).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}):'Unknown';
function face(a){
 const wrap=node('span','face'),img=node('img');img.src=window.AGENTSTACK_DEMO.portraitURL(a.name);img.alt='';
 img.onerror=()=>{img.remove();wrap.prepend(node('span','initials',a.name.slice(0,2)));};wrap.appendChild(img);
 const provider=window.OrreryNetworkNode.providerKeyFor({provider:a.provider,model:a.model,locality:a.locality});
 if(provider){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('class','badge node');svg.setAttribute('viewBox','-10 -10 20 20');svg.setAttribute('aria-hidden','true');const badge=window.OrreryNetworkNode.providerBadge(provider,0,0,18);if(badge){svg.appendChild(badge);wrap.appendChild(svg);}}
 return wrap;
}
function person(name){const a=snapshot.agents.find(x=>x.name===name)||{name,provider:'',model:'',locality:null};const e=node('div','person');e.append(face(a),node('span','name',name));return e;}
function showView(view){
 clearDetail();clearPair();
 const graph=view==='graph';$('#activity').hidden=graph;$('#graph-panel').hidden=!graph;
 if(!graph)graphFullscreen?.exit();
 document.querySelectorAll('[data-view]').forEach(x=>x.classList.toggle('active',x.dataset.view===view));
 if(graph){network.applyView();network.hot=Math.max(network.hot,120);network.run();}else network.pause();
}
function renderActivity(){
 if(!snapshot)return;
 const mail=mailPage?.events||snapshot.timeline.mail;
 const events=[...mail.map(x=>({...x,type:'mail',sort:x.event_at})),...(mailPage?[]:snapshot.timeline.states).map(x=>({...x,type:'state',sort:x.observed_at}))].sort((a,b)=>b.sort-a.sort);
 const container=$('#events');container.replaceChildren();
 const messages={work:'Started working',wait:'Now waiting',ask:'Awaiting review',question:'Needs an answer',finished:'Finished',gone:'Stopped',retired:'Retired',idle:'Idle'};
 for(const e of events){
  if(kind!=='all'&&e.type!==kind)continue;
  if(agentFilter&&![e.source,e.target,e.name].includes(agentFilter))continue;
  const article=node('article','event'+(e.to==='ask'||e.to==='question'?' review':''));
  const stamp=node('time','',time(e.sort)+(e.type==='state'?' · observed':''));article.appendChild(stamp);
  if(e.type==='mail'){
   article.appendChild(mailButton(e,mailPage?.window||snapshot.window));
  }else{
   const row=node('div','status-line'+(e.to==='ask'||e.to==='question'?' review':''));markRef(row,e);const who=snapshot.agents.find(a=>a.name===e.name)||{name:e.name};row.appendChild(face(who));const copy=node('div');copy.append(node('div','name',e.name),node('p','',messages[e.to]||'Status changed'));row.appendChild(copy);article.appendChild(row);
  }
  container.appendChild(article);
 }
 if(!container.children.length)container.appendChild(node('p','empty',snapshot.sources.mail.status==='unavailable'?'Mail observations unavailable':'No matching events observed'));
 const notes=['Mail shows to/cc recipient events. Status changes were observed after this reader started.'];
 if(snapshot.timeline.events_truncated||mailPage)notes.push('Up to 200 Mail recipient events per page. Older/Newer replaces this page.');
 if(snapshot.timeline.edges_truncated)notes.push('Graph is limited to 10,000 Mail links; period counts are incomplete.');
 else if(snapshot.timeline.events_truncated)notes.push('Graph counts cover the full selected period.');
 if(snapshot.timeline.states_truncated)notes.push('Only the latest 200 observed status changes are retained.');
 if(snapshot.sources.mail.status==='stale')notes.push('Mail observations are stale.');
 $('#timeline-note').textContent=notes.join(' ');
 $('#mail-older').disabled=!(mailPage?mailPage.next_cursor:snapshot.timeline.next_cursor)||!!mailController;
 $('#mail-newer').disabled=mailPosition===0||!!mailController;
 $('#mail-page-label').textContent=mailPage?'Older Mail · Status stays on latest page':'Latest Mail';
 const cache=snapshot.mail_cache;
 $('#mail-cache-note').textContent=cache?'Mac 24h cache: '+cache.status+' · '+cache.messages+' messages. Cache coverage does not limit older message viewing.':'';
 $('#agent-filter').hidden=!agentFilter;$('#agent-filter').textContent=agentFilter+' ×';
}
function showNotices(notices,data,label){
 lastNotices=notices;
 const notice=$('#observation-notice');notice.hidden=notices.length===0;
 if(notice.hidden)notice.open=false;
 const sources=data?.sources,unknown=data?.period?.activity_unknown;
 if(!label){
  if(sources&&[sources.agents,sources.graph].some(s=>s.status==='unavailable'))label='Unavailable';
  else if(sources?.mail.status==='unavailable')label='Mail unavailable';
  else if(sources&&Object.values(sources).some(s=>s.status==='stale'))label='Stale';
  else if(unknown)label=unknown+(data.period.seconds===null?' unknown':' excluded');
  else label='Observation notes';
 }
 $('#health-label').textContent=label;
 $('#health').textContent=notices.join(' ');
 $('#snapshot-period-note').textContent=data?.period?'Snapshot period: '+duration(data.period.seconds)+'.':'No snapshot has been observed yet.';
}
function render(data){
 if(snapshot?.timeline.scope&&snapshot.timeline.scope!==data.timeline.scope){clearDetail();clearPair();resetMail();}
 snapshot=data;receivedAt=performance.now();
 // The displayed roster is the projection's graph.nodes + agents union.
 // Mail success cannot establish an empty roster or working/waiting counts.
 const rosterSources=[data.sources.agents,data.sources.graph],mail=data.sources.mail;
 const rosterObserved=rosterSources.some(s=>s.observed_at!==null&&s.status!=='unavailable');
 const rosterFresh=rosterSources.every(s=>s.status==='available');
 const working=data.agents.filter(a=>a.running&&a.act_state==='work').length,waiting=data.agents.filter(a=>a.running&&a.act_state==='wait').length;
 const summary=$('#summary');summary.replaceChildren();
 if(!rosterObserved)summary.textContent='Agent counts unavailable';
 else{
  if(!rosterFresh)summary.appendChild(document.createTextNode('Last known: '));
  summary.append(node('b','',working),document.createTextNode(' working / '),node('b','',waiting),document.createTextNode(' waiting'));
 }
 $('#observed').textContent=time(data.observed_at);
 const notices=[];
 if(!rosterObserved)notices.push('Agent observations unavailable.');
 else if(!rosterFresh)notices.push('Agent observations are stale or incomplete. Counts are last known.');
 if(mail.status==='unavailable')notices.push('Mail observations unavailable.');
 else if(mail.status==='stale')notices.push('Mail observations are stale.');
 else if(!rosterObserved)notices.push('Mail metadata is available.');
 if(data.timeline.edges_truncated)notices.push('Graph is limited to 10,000 Mail links. Period counts are incomplete.');
 else if(data.graph.degraded)notices.push('Some graph metadata could not be read.');
 if(rosterObserved&&data.graph.unrepresented_edges)notices.push('Some Mail endpoints are outside the current roster.');
 if(data.period?.activity_unknown){const n=data.period.activity_unknown;notices.push(n+(n===1?' agent has an unknown activity time':' agents have unknown activity times')+(data.period.seconds===null?'.':n===1?' and is excluded from this period.':' and are excluded from this period.'));}
 showNotices(notices,data);
 const attention=$('#attention');attention.replaceChildren();
 const needs=data.agents.filter(a=>['ask','question'].includes(a.act_state)||a.state==='unknown');
 for(const a of needs.slice(0,3)){
  const row=node('button','attention-row '+(['ask','question'].includes(a.act_state)?'review':'unknown'));row.type='button';
  markRef(row,a);
  row.appendChild(face(a));const copy=node('span','copy');copy.append(node('span','name',a.name),node('span','reason',a.act_state==='ask'?'Awaiting review':a.act_state==='question'?'Needs an answer':a.last_active?'Unknown · last active '+time(a.last_active):'Observation unavailable'));row.append(copy,node('span','alert-dot'),node('span','chevron','›'));
  row.onclick=()=>{agentFilter=a.name;kind='all';syncFilters();showView('activity');renderActivity();$('#activity').scrollIntoView({block:'start'});};attention.appendChild(row);
 }
 if(needs.length>3)attention.appendChild(node('p','quiet',(needs.length-3)+' more agents need attention. Use Find in Graph to inspect.'));
 renderActivity();network.update(data);
 $('#snapshot-note').textContent='Snapshot observed '+time(data.observed_at)+'. Account usage unavailable. Context arcs are separate.';
}
function syncFilters(){document.querySelectorAll('[data-kind]').forEach(b=>b.classList.toggle('active',b.dataset.kind===kind));}
function markStale(message){
 if(snapshot){
  const elapsed=(performance.now()-receivedAt)/1000;
  const retained=structuredClone(snapshot);
  for(const source of Object.values(retained.sources)){
   if(source.status!=='unavailable')source.status='stale';
   if(source.age_seconds!==null)source.age_seconds+=elapsed;
  }
  render(retained);
 }
 showNotices([message,...(snapshot?lastNotices:[])],snapshot,message.includes('unavailable')?'Unavailable':'Stale');
}
async function poll(){
 if(document.hidden)return;
 if(active){pending=true;return;}
 const controller=new AbortController(),windowAtStart=desiredWindow;active=controller;
 const timeout=setTimeout(()=>controller.abort(),8000);
 try{
  const response=await fetch('/view/snapshot?window='+windowAtStart,{signal:controller.signal,cache:'no-store',credentials:'same-origin',referrerPolicy:'no-referrer'});
  const data=await response.json();
  if(windowAtStart!==desiredWindow){pending=true;return;}
  if(data.schema!==1)throw Error('unavailable');
  render(data);
 }catch(_){
  if(windowAtStart===desiredWindow)markStale('Observations unavailable. The last snapshot is retained.');
 }finally{
  clearTimeout(timeout);active=null;clearTimeout(timer);
  if(!document.hidden){const soon=pending;pending=false;timer=setTimeout(poll,soon?0:10000);}
 }
}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>showView(b.dataset.view));
document.querySelectorAll('[data-kind]').forEach(b=>b.onclick=()=>{kind=b.dataset.kind;syncFilters();renderActivity();});
$('#agent-filter').onclick=()=>{agentFilter='';renderActivity();};
let previousFocus=null;
function settings(open){
 $('#settings').hidden=!open;$('#settings').setAttribute('aria-hidden',String(!open));$('#settings-btn').setAttribute('aria-expanded',String(open));
 if(open){previousFocus=document.activeElement;$('#settings-close').focus();}else previousFocus?.focus();
}
$('#settings-btn').onclick=()=>settings($('#settings').hidden);$('#settings-close').onclick=()=>settings(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#settings').hidden)settings(false);});
let periodTimer=0;
const range=$('#time-window');
const periodSheet=$('#period-sheet'),periodButtons=[...document.querySelectorAll('[data-period]')];
let periodFocus=null;
function openPeriod(){
 if(!$('#settings').hidden)settings(false);
 periodFocus=document.activeElement;
 periodSheet.showModal();document.documentElement.classList.add('period-open');
 periodButtons.forEach(b=>b.setAttribute('aria-expanded','true'));
 (range.disabled?$('#period-close'):range).focus({preventScroll:true});
}
periodButtons.forEach(b=>b.onclick=openPeriod);
$('#period-close').onclick=()=>periodSheet.close();
periodSheet.addEventListener('keydown',e=>{
 if(e.key!=='Tab')return;
 const stops=[...periodSheet.querySelectorAll('button,input')].filter(x=>!x.disabled),first=stops[0],last=stops[stops.length-1];
 if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
 else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
});
periodSheet.addEventListener('click',e=>{
 const r=periodSheet.getBoundingClientRect();
 if(e.target===periodSheet&&(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom))periodSheet.close();
});
periodSheet.addEventListener('close',()=>{
 document.documentElement.classList.remove('period-open');
 periodButtons.forEach(b=>b.setAttribute('aria-expanded','false'));
 if(periodFocus?.isConnected)periodFocus.focus({preventScroll:true});
});
function periodLabel(text){
 $('#period-label').textContent=text;
 periodButtons.forEach(b=>{b.querySelector('span').textContent=text.replace('Last ','').replace(' min','m');b.setAttribute('aria-label','Change time range: '+text);});
}
range.value=Math.round(Math.log(1440/15)/Math.log(43200/15)*1000);
range.setAttribute('aria-valuetext',duration(86400));
function choosePeriod(all){
 clearDetail();clearPair();resetMail();
 const minutes=minutesAt(Number(range.value));
 desiredWindow=all?'all':minutes===1440?'day':'m'+minutes;
 range.disabled=all;$('#all-history').setAttribute('aria-pressed',String(all));
 periodLabel(duration(all?null:minutes*60));
 range.setAttribute('aria-valuetext',duration(minutes*60));
 clearTimeout(periodTimer);clearTimeout(timer);
 periodTimer=setTimeout(poll,200);
}
range.oninput=()=>choosePeriod(false);
$('#all-history').onclick=()=>choosePeriod(desiredWindow!=='all');
document.addEventListener('visibilitychange',()=>{
 clearTimeout(timer);if(document.hidden){clearDetail();clearPair();cancelMail();active?.abort();network.pause();}else{poll();network.run();}
});
window.addEventListener('pagehide',()=>{clearDetail();clearPair();cancelMail();active?.abort();});

function mailButton(event,window){
 const route=node('button','mail-route');route.type='button';route.dataset.event=event.key;
 route.appendChild(person(event.source));const to=person(event.target);to.classList.add('route-to');route.appendChild(to);
 route.append(node('span','route-kind',event.kind==='cc'?'Cc recipient':'To recipient'),node('span','open-message','Read message ›'));
 route.onclick=()=>openMessage(event,window);return route;
}
function clearDetail(close=true){
 document.querySelector('.terminal-mail-people')?.remove();
 detailGeneration++;detailController?.abort();detailController=null;
 for(const id of ['message-route','message-status','message-subject','message-body'])$('#'+id).textContent='';
 $('#message-title').textContent='Message';
 if(close&&$('#message-sheet').open)$('#message-sheet').close();
}
async function openMessage(event,window){
 clearDetail(false);const generation=detailGeneration,controller=new AbortController();detailController=controller;
 const sheet=$('#message-sheet');if(!sheet.open)sheet.showModal();
 $('#message-route').textContent=event.source+' → '+event.target+' · '+time(event.event_at)+' · '+event.kind;
 globalThis.OrreryMobileTerminal?.mail(event);
 $('#message-status').textContent='Loading one message…';
 const timeout=setTimeout(()=>controller.abort(),8000);
 try{
  const response=await fetch('/view/message?'+new URLSearchParams({window,event:event.key}),{signal:controller.signal,cache:'no-store',credentials:'same-origin',referrerPolicy:'no-referrer'});
  const data=await response.json();
  if(generation!==detailGeneration||document.hidden||!sheet.open)return;
  if(!response.ok||!data.event||['key','source','target','kind','event_at'].some(k=>data.event[k]!==event[k])||typeof data.subject!=='string'||typeof data.body!=='string')throw Error('unavailable');
  $('#message-title').textContent='Message';$('#message-subject').textContent=data.subject||'(No subject)';$('#message-body').textContent=data.body;
  $('#message-status').textContent=[data.subject_truncated?'Subject shortened to 512 characters.':'',data.body_truncated?'Body shortened to 64 KiB.':'','Read only · no read receipt or acknowledgement sent.'].filter(Boolean).join(' ');
 }catch(_){if(generation===detailGeneration&&sheet.open){$('#message-title').textContent='Message unavailable';$('#message-status').textContent='This event expired, changed or could not be read. Close and refresh its metadata.';}}
 finally{clearTimeout(timeout);if(generation===detailGeneration)detailController=null;}
}
$('#message-close').onclick=()=>clearDetail();
$('#message-sheet').addEventListener('close',()=>{if(!$('#message-sheet').open)clearDetail(false);});
function cancelMail(){mailGeneration++;mailController?.abort();mailController=null;}
function resetMail(){cancelMail();mailPage=null;mailPositions=[null];mailPosition=0;$('#mail-page-error').textContent='';}
async function mailMove(older){
 if(!snapshot)return;
 clearDetail();cancelMail();
 const positions=mailPositions.slice();let position=mailPosition;
 if(older){const next=mailPage?mailPage.next_cursor:snapshot.timeline.next_cursor;if(!next)return;positions.splice(position+1);positions.push(next);position++;if(positions.length>20){positions.shift();position--;}}
 else{if(!position)return;position--;}
 const cursor=positions[position];
 if(cursor===null){mailPage=null;mailPosition=position;mailPositions=positions;renderActivity();$('#activity').scrollIntoView({block:'start',behavior:'instant'});return;}
 const generation=mailGeneration,window=snapshot.window,controller=new AbortController();mailController=controller;renderActivity();
 const timeout=setTimeout(()=>controller.abort(),8000);
 try{
  const response=await fetch('/view/mail?'+new URLSearchParams({window,cursor}),{signal:controller.signal,cache:'no-store',credentials:'same-origin'});const data=await response.json();
  if(generation!==mailGeneration||document.hidden)return;
  if(!response.ok||data.window!==window||!Array.isArray(data.events))throw Error('page unavailable');
  mailPage=data;mailPositions=positions;mailPosition=position;$('#mail-page-error').textContent='';renderActivity();$('#activity').scrollIntoView({block:'start',behavior:'instant'});
 }catch(_){if(generation===mailGeneration)$('#mail-page-error').textContent='Mail page unavailable or expired. Your current page is retained.';}
 finally{clearTimeout(timeout);if(generation===mailGeneration){mailController=null;renderActivity();}}
}
$('#mail-older').onclick=()=>mailMove(true);$('#mail-newer').onclick=()=>mailMove(false);
function clearPair(close=true){
 pairGeneration++;pairController?.abort();pairController=null;pair=null;
 $('#pair-events').replaceChildren();$('#pair-status').textContent='';
 if(close&&$('#pair-sheet').open)$('#pair-sheet').close();
}
function openPair(a,b){
 if(!snapshot)return;
 clearDetail();clearPair(false);
 pair={a,b,direction:'both',window:snapshot.window,positions:[null],position:0,data:null};
 $('#pair-title').textContent=a+' ↔ '+b;$('#pair-period').textContent=duration(snapshot.period.seconds)+' · To/Cc recipient events';
 const buttons=[...$('#pair-directions').querySelectorAll('button')];
 buttons[1].textContent=a+' → '+b;buttons[2].textContent=b+' → '+a;buttons[1].hidden=buttons[2].hidden=a===b;
 if(!$('#pair-sheet').open)$('#pair-sheet').showModal();
 pairLoad();
}
async function pairLoad(move=0){
 if(!pair)return;clearDetail();pairGeneration++;pairController?.abort();
 const generation=pairGeneration,context=pair,positions=context.positions.slice();let position=context.position;
 if(move>0){if(!context.data?.next_cursor)return;positions.splice(position+1);positions.push(context.data.next_cursor);position++;if(positions.length>20){positions.shift();position--;}}
 else if(move<0){if(!position)return;position--;}
 const controller=new AbortController();pairController=controller;$('#pair-status').textContent='Loading metadata…';
 $('#pair-newer').disabled=$('#pair-older').disabled=true;
 for(const button of $('#pair-directions').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.dataset.direction===context.direction));
 const query=new URLSearchParams({window:context.window,a:context.a,b:context.b,direction:context.direction});
 if(positions[position])query.set('cursor',positions[position]);
 const timeout=setTimeout(()=>controller.abort(),8000);
 try{
  const response=await fetch('/view/mail?'+query,{signal:controller.signal,cache:'no-store',credentials:'same-origin'});const data=await response.json();
  if(generation!==pairGeneration||pair!==context||document.hidden)return;
  if(!response.ok||data.window!==context.window||data.direction!==context.direction||!Array.isArray(data.events))throw Error('unavailable');
  context.data=data;context.positions=positions;context.position=position;$('#pair-sheet').scrollTop=0;
  const container=$('#pair-events');container.replaceChildren();
  for(const event of data.events){const article=node('article','event');article.append(node('time','',time(event.event_at)),mailButton(event,context.window));container.appendChild(article);}
  $('#pair-status').textContent=data.events.length?(data.next_cursor?'Up to 200 events. More metadata is available.':'End of matching Mail metadata.'):'No matching Mail events observed.';
 }catch(_){if(generation===pairGeneration)$('#pair-status').textContent='Pair metadata unavailable or expired. Previous matching page is retained if present.';}
 finally{clearTimeout(timeout);if(generation===pairGeneration){pairController=null;$('#pair-newer').disabled=!context.position;$('#pair-older').disabled=!context.data?.next_cursor;$('#pair-page-label').textContent=context.position?'Older Mail':'Latest matching Mail';}}
}
$('#pair-close').onclick=()=>{clearDetail();clearPair();};
$('#pair-sheet').addEventListener('close',()=>{if(!$('#pair-sheet').open){clearDetail();clearPair(false);}});
$('#pair-directions').querySelectorAll('button').forEach(button=>button.onclick=()=>{
 if(!pair||pair.direction===button.dataset.direction)return;clearDetail();pair.direction=button.dataset.direction;pair.positions=[null];pair.position=0;pair.data=null;$('#pair-events').replaceChildren();pairLoad();
});
$('#pair-newer').onclick=()=>pairLoad(-1);$('#pair-older').onclick=()=>pairLoad(1);
// Browser clocks can differ from the host; age advances from the reported
// observation age plus local elapsed time, not by pretending clocks agree.
setInterval(()=>{
 if(!snapshot||document.hidden)return;
 const elapsed=(performance.now()-receivedAt)/1000;
 if(Object.values(snapshot.sources).some(s=>s.age_seconds!==null&&s.age_seconds+elapsed>30)){
  markStale('Observations are stale. Showing the last snapshot.');
 }
},5000);
// Fullscreen the body, not the SVG/panel: modal sheets must remain descendants
// of the native fullscreen element. Unsupported/rejected API uses CSS only.
function initGraphFullscreen(){
 const root=document.documentElement,button=$('#graph-fullscreen');
 const notice=$('#observation-notice'),noticeAnchor=document.createComment('observation position');notice.before(noticeAnchor);
 let active=false,nativeOwned=false,generation=0,sequence=0,entry=0,scroll=null;
 let oldRestoration=null,size=network.dimensions();
 const ownHistory=()=>entry&&history.state?.orreryGraphFullscreen===entry;
 const closeTop=()=>{
  const dialogs=[...document.querySelectorAll('dialog[open]')],top=dialogs.at(-1);
  if(top){if(top.id==='terminal-sheet')window.OrreryMobileTerminal.close();else top.close();return true;}
  if(!$('#settings').hidden){settings(false);return true;}
  if(notice.open){notice.open=false;return true;}
  return false;
 };
 const resize=()=>{
  const next=network.dimensions();
  if(next.w===size.w&&next.h===size.h)return;
  if(network.userView){
   const v=network.view,cx=v.vx+size.w/v.k/2,cy=v.vy+size.h/v.k/2;
   network.view={k:v.k,vx:cx-next.w/v.k/2,vy:cy-next.h/v.k/2};network.applyView();
  }else network.fit();
  size=next;
 };
 new ResizeObserver(resize).observe(network.net);
 function exit(pop=true){
  if(!active)return;
  const remove=pop&&ownHistory(),restore=scroll,restoration=oldRestoration,g=++generation;active=false;nativeOwned=false;
  root.classList.remove('graph-fullscreen');delete root.dataset.graphDisplay;
  notice.open=false;noticeAnchor.after(notice);
  button.textContent='Expand';button.setAttribute('aria-label','Expand graph');button.setAttribute('aria-expanded','false');
  const finished=document.fullscreenElement===document.body?document.exitFullscreen().catch(()=>{}):Promise.resolve();
  if(restoration!==null)history.scrollRestoration=restoration;
  if(remove)history.back();
  // Native exit can reset scrolling after the first frame. Restore only once
  // it has settled, and never let an older exit move a newly expanded graph.
  finished.then(()=>requestAnimationFrame(()=>requestAnimationFrame(()=>{
   if(active||g!==generation)return;
   resize();if(restore)window.scrollTo({left:restore.x,top:restore.y,behavior:'instant'});
   button.focus({preventScroll:true});
  })));
 }
 function enter(){
  if(active)return;active=true;nativeOwned=false;const g=++generation;
  size=network.dimensions();scroll={x:scrollX,y:scrollY};
  oldRestoration=history.scrollRestoration;entry=++sequence;
  // Mode only: no agent, target proof, draft, preview or intent in history/URL.
  history.pushState({orreryGraphFullscreen:entry},'');
  history.scrollRestoration='manual';
  root.classList.add('graph-fullscreen');root.dataset.graphDisplay='css';
  $('#netinfo').after(notice);
  button.textContent='Close';button.setAttribute('aria-label','Close expanded graph');button.setAttribute('aria-expanded','true');
  requestAnimationFrame(resize);
  if(document.fullscreenEnabled&&typeof document.body.requestFullscreen==='function'){
   try{
    Promise.resolve(document.body.requestFullscreen()).then(()=>{
     if(!active){if(document.fullscreenElement===document.body)document.exitFullscreen().catch(()=>{});return;}
     if(g!==generation)return;
     nativeOwned=document.fullscreenElement===document.body;
     if(nativeOwned)root.dataset.graphDisplay='native';
    }).catch(()=>{});
   }catch(_){} // CSS expansion already works; never change permissions.
  }
 }
 button.onclick=()=>active?exit():enter();
 window.addEventListener('popstate',()=>{
  if(!active)return;
  // Back closes the foreground sheet first, retaining the graph mode entry.
  if(closeTop()){history.pushState({orreryGraphFullscreen:entry},'');return;}
  exit(false);
 });
 document.addEventListener('keydown',e=>{
  if(!active||e.key!=='Escape')return;e.preventDefault();e.stopImmediatePropagation();
  if(!closeTop())exit();
 },true);
 document.addEventListener('fullscreenchange',()=>{
  if(document.fullscreenElement===document.body){if(active){nativeOwned=true;root.dataset.graphDisplay='native';}return;}
  if(active&&nativeOwned){while(closeTop()){}exit();}
 });
 window.addEventListener('pagehide',()=>{if(active){while(closeTop()){}exit(false);}});
 return {exit};
}
graphFullscreen=initGraphFullscreen();
poll();
})();

/* Common, opt-in pane terminal. No preview/draft/intent enters storage or URLs. */
(()=>{'use strict';
const q=s=>document.querySelector(s),make=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!==undefined)e.textContent=txt;return e;};
const sheet=q('#terminal-sheet'),contract='orrery-mobile-pane-v1';
let capability=null,enabled=false,phase='loading',name='',choices=[],choice=null,target=null,text='',output='',preview=null,previewOK=false,message='';
let selector=null;
const fresh=()=>capability?.api===34;
const ref=value=>value&&/^[a-f0-9]{32}$/.test(value.mail_instance_id)&&Number.isSafeInteger(value.mail_agent_id)&&value.mail_agent_id>0?{mail_instance_id:value.mail_instance_id,mail_agent_id:value.mail_agent_id}:null;
const rowRef=row=>ref({mail_instance_id:row.dataset.mailInstanceId,mail_agent_id:Number(row.dataset.mailAgentId)});
const sameTarget=value=>!fresh()||!!ref(value)&&!!selector&&value.mail_instance_id===selector.mail_instance_id&&value.mail_agent_id===selector.mail_agent_id;
let generation=0,previewController=null,operationController=null,pollTimer=0,expiryTimer=0,failures=0,prepared=null,receipt=null,previousFocus=null;
const action=(label,cls,fn)=>{const b=make('button','terminal-button '+cls,label);b.type='button';b.onclick=fn;return b;};
async function request(operation,payload,controller,timeout=3000){
 const timer=setTimeout(()=>controller.abort(),timeout);
 try{
  const response=await fetch('/view/terminal'+(operation?'/'+operation:''),{method:operation?'POST':'GET',
   headers:operation?{'Content-Type':'application/json'}:{},body:operation?JSON.stringify(payload):undefined,
   credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',signal:controller.signal});
  const raw=await response.text();if(new TextEncoder().encode(raw).length>16384)throw Error('unavailable');
  const value=JSON.parse(raw);
  if(!response.ok||value.contract!==contract||operation&&value.epoch!==capability.epoch)throw Error('unavailable');
  return value;
 }finally{clearTimeout(timer);}
}
function cancelIntent(){
 const old=prepared||receipt;prepared=null;receipt=null;clearTimeout(expiryTimer);
 if(!old||!enabled)return;
 const controller=new AbortController();
 // Best effort cancellation never retries commit, and keeps no text or preview.
 request('cancel',{epoch:old.epoch,intent:old.intent},controller,2000).catch(()=>{});
}
function stop(){clearTimeout(pollTimer);pollTimer=0;previewController?.abort();previewController=null;operationController?.abort();operationController=null;generation++;}
function clear(){stop();cancelIntent();name='';selector=null;choices=[];choice=null;target=null;text='';output='';preview=null;previewOK=false;message='';phase='loading';failures=0;sheet.replaceChildren();}
function close(){const restore=previousFocus;previousFocus=null;clear();if(sheet.open)sheet.close();if(!document.hidden&&restore?.isConnected)restore.focus({preventScroll:true});}
function entry(n,cls='',identity=null){
 const b=make('button','terminal-entry '+cls,'Terminal');b.type='button';b.dataset.terminalName=n;
 b.setAttribute('aria-label','Open Terminal for '+n);b.disabled=!enabled;b.title=enabled?'View this agent’s selectable cockpit panes':'Terminal is off. Designated-phone policy acceptance and explicit opt-in are required.';
 b.onclick=()=>open(n,identity);return b;
}
const graphEntry=entry('','terminal-graph-entry');graphEntry.id='terminal-graph-entry';q('.selected-agent').after(graphEntry);
function entries(){
 const selected=q('#selected-name').textContent;graphEntry.replaceChildren(make('span','',selected==='None'?'Select an agent':selected),make('strong','','Terminal ⌁'));graphEntry.disabled=!enabled||selected==='None';graphEntry.onclick=()=>open(selected,window.OrreryMobileIdentity?.forName(selected));
 q('#attention').querySelectorAll(':scope > .attention-row').forEach(row=>{const n=row.querySelector('.name')?.textContent;if(!n)return;const wrap=make('div','terminal-attention');row.replaceWith(wrap);wrap.append(row,entry(n,'',rowRef(row)));});
 q('#events').querySelectorAll('.status-line').forEach(row=>{const n=row.querySelector('.name')?.textContent;if(n&&!row.querySelector('.terminal-entry'))row.append(entry(n,'',rowRef(row)));});
 document.querySelectorAll('.terminal-entry:not(#terminal-graph-entry)').forEach(b=>b.disabled=!enabled);
}
new MutationObserver(entries).observe(q('#selected-name'),{childList:true,subtree:true});
new MutationObserver(entries).observe(q('#attention'),{childList:true});new MutationObserver(entries).observe(q('#events'),{childList:true});
function mail(event){
 q('.terminal-mail-people')?.remove();const people=make('div','terminal-mail-people');
 for(const [label,n,id]of[['From',event.source,event.source_agent_id],['To',event.target,event.target_agent_id]]){const row=make('div','terminal-mail-person');row.append(make('span','',label+' '+n),entry(n,'',ref({mail_instance_id:event.mail_instance_id,mail_agent_id:id})));people.append(row);}
 q('#message-route').after(people);
}
async function open(n,identity=null){
 if(!enabled||document.hidden)return;previousFocus=sheet.open?previousFocus:document.activeElement;clear();name=n;selector=ref(identity);window.scrollTo({top:0,behavior:'instant'});
 if(!sheet.open)sheet.showModal();
 if(fresh()&&!selector){phase='unavailable';message='A unique Mail identity is required. Refresh the agent list or open a Mail participant.';render();return;}
 render();await list();
}
async function list(){
 stop();cancelIntent();choices=[];choice=null;target=null;text='';output='';preview=null;previewOK=false;phase='loading';message='';render();
 const g=generation,controller=new AbortController();operationController=controller;
 try{
  if(fresh()&&!selector)throw Error('unavailable');
  const data=await request('targets',{epoch:capability.epoch,name,...(fresh()?selector:{})},controller);
  if(g!==generation||document.hidden||!sheet.open)return;
  if(!Array.isArray(data.targets)||data.targets.length>128||data.targets.some(t=>t.name!==name||!sameTarget(t)||typeof t.choice!=='string'||typeof t.pane_label!=='string'))throw Error('unavailable');
  choices=data.targets;message=data.truncated?'Some panes are omitted. Refresh to see available choices.':'';
  if(choices.length===1){choose(choices[0]);return;}
  phase=choices.length?'choose':'unavailable';render();
 }catch(_){if(g===generation&&sheet.open){phase='unavailable';message='No selectable cockpit pane could be verified. Refresh panes to try again.';render();}}
 finally{if(g===generation)operationController=null;}
}
function choose(value){
 stop();cancelIntent();choice=value;target=null;text='';output='';preview=null;previewOK=false;phase='editing';message='';failures=0;render();poll();
}
function terminalFollowing(pre){
 const width=pre.clientWidth,height=pre.clientHeight,old=pre._scrollView;
 // Keyboard/rotation reflow is not a reader scrolling away from the tail.
 const resized=old&&(old.width!==width||old.height!==height);
 const atBottom=pre.scrollHeight-height-pre.scrollTop<=2;
 // A resize can clamp a reader onto the new DOM bottom. Keep that pause
 // through later scroll events/polls until a downward gesture or away/back.
 const clamped=atBottom&&Boolean(resized?!old.following:old?.clamped);
 const following=clamped?false:resized?old.following:atBottom;
 pre._scrollView={width,height,following,clamped};
 return following;
}
function terminalScrollGesture(pre,down){
 pre._towardBottom=Boolean(down);
 if(pre._inputFramePending)return;
 pre._inputFramePending=true;
 requestAnimationFrame(()=>{
  pre._inputFramePending=false;
  const latest=pre._towardBottom;pre._towardBottom=false;
  // Several inputs can precede one frame. Only their last direction and
  // the position after native scrolling may release a resize-clamped reader.
  if(q('#terminal-output')!==pre||!sheet.open||!latest||pre.scrollHeight-pre.clientHeight-pre.scrollTop>2)return;
  pre._scrollView=null;refreshOutput();
 });
}
function refreshOutput(){
 const pre=q('#terminal-output');if(pre){
  // A preview is a moving tail, so keeping scrollTop alone still moves the
  // words being read. Hold the displayed tail until the reader reaches bottom.
  const top=pre.scrollTop,atBottom=terminalFollowing(pre);
  if(atBottom||!previewOK){
   if(pre.textContent!==output)pre.textContent=output;
   pre._preview=preview;
   pre.scrollTop=atBottom?pre.scrollHeight:top;
   if(!previewOK)pre._scrollView=null;
  }
 }
 const shown=pre?pre._preview:null,reading=pre&&pre._scrollView&&!pre._scrollView.following;
 const pending=previewOK&&shown&&shown.text!==output;
 const stream=q('.terminal-stream');if(stream)stream.textContent=previewOK?(pending?'New output · paused':reading?'Reading earlier output':'Updating every 3s'):'Preview unavailable';
 const age=q('.terminal-age');if(age)age.textContent=shown?'Shown '+new Date(shown.observed_at*1000).toLocaleTimeString('en-GB'):'';
 const note=q('.terminal-preview-note');if(note)note.textContent=[shown?.truncated?'Tail shortened to 6 KiB.':'',shown?.controls_removed?'Control sequences removed.':'',pending?'Scroll to bottom to show the latest tail. Output between snapshots (40 lines / 6 KiB) may be missing.':''].filter(Boolean).join(' ');
 const review=q('#terminal-review');if(review)review.disabled=phase!=='editing'||!previewOK||!validText(text);
 const status=q('.terminal-message');if(status)status.textContent=message;
}
async function poll(){
 if(!sheet.open||document.hidden||!choice||previewController)return;
 const g=generation,c=choice,controller=new AbortController();previewController=controller;
 try{
  const data=await request('preview',{epoch:capability.epoch,choice:c.choice},controller);
  if(g!==generation||previewController!==controller||document.hidden||!sheet.open||choice!==c)return;
  if(data.target?.choice!==c.choice||data.target?.name!==name||!sameTarget(data.target)||typeof data.target?.revision!=='string'||typeof data.text!=='string'||new TextEncoder().encode(data.text).length>6144)throw Error('unavailable');
  if(target&&data.target.revision!==target.revision){invalidate('Pane changed. Refresh panes and review again.');return;}
  target=Object.freeze({...data.target});preview=data;output=data.text;previewOK=true;failures=0;
  if(!['preparing','review','sending','accepted','unknown'].includes(phase))message='';
  refreshOutput();
 }catch(_){
  if(g!==generation||previewController!==controller||!sheet.open||document.hidden)return;
  failures++;previewOK=false;output='';preview=null;
  if(phase==='review'){cancelIntent();phase='editing';message='Preview unavailable. Review again after a fresh update.';render();}
  else{message='Preview unavailable. Refresh panes if the target changed.';refreshOutput();}
 }finally{
  if(g===generation&&previewController===controller){previewController=null;if(sheet.open&&!document.hidden&&choice)pollTimer=setTimeout(poll,failures?Math.min(24000,3000*2**failures):3000);}
 }
}
function invalidate(reason){stop();cancelIntent();text='';output='';preview=null;previewOK=false;target=null;phase='changed';message=reason;render();}
function validText(value){return typeof value==='string'&&value.trim()&&new TextEncoder().encode(value).length<=4096&&!/[\p{Cc}\p{Cs}\u2028\u2029\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/u.test(value);}
async function reviewInput(){
 if(!target||!choice||!previewOK||!validText(text))return;
 clearTimeout(pollTimer);previewController?.abort();previewController=null;
 const g=generation,t=target,draft=text,controller=new AbortController();operationController=controller;phase='preparing';render();
 try{
  const data=await request('prepare',{epoch:capability.epoch,choice:t.choice,revision:t.revision,text:draft},controller);
  if(g!==generation||!sheet.open||document.hidden)return;
  if(data.state!=='prepared'||data.text!==draft||data.expires_in!==30||data.target?.revision!==t.revision||data.target?.choice!==t.choice||data.target?.name!==name||!sameTarget(data.target))throw Error('unavailable');
  prepared=Object.freeze({...data,target:Object.freeze({...data.target})});receipt=null;phase='review';message='';render();
  const identity=prepared;expiryTimer=setTimeout(()=>{if(prepared!==identity)return;cancelIntent();phase='editing';message='Confirmation expired. Review again.';render();},data.expires_in*1000);
 }catch(_){if(g===generation&&sheet.open){prepared=null;phase='editing';message='Could not prepare this input. Refresh the pane and review again.';render();}}
 finally{if(g===generation){operationController=null;if(!document.hidden&&sheet.open)pollTimer=setTimeout(poll,3000);}}
}
async function send(){
 if(phase!=='review'||!prepared||!previewOK)return;
 const p={epoch:prepared.epoch,intent:prepared.intent,confirmation:prepared.confirmation,target:prepared.target};
 prepared=null;receipt=p;text='';clearTimeout(expiryTimer);clearTimeout(pollTimer);previewController?.abort();previewController=null;
 const g=generation,controller=new AbortController();operationController=controller;phase='sending';message='';render();
 try{
  const result=await request('commit',{epoch:p.epoch,intent:p.intent,confirmation:p.confirmation},controller,7000);
  if(g!==generation||!sheet.open||document.hidden)return;
  if(result.intent!==p.intent||result.target?.revision!==p.target.revision||!sameTarget(result.target))throw Error('unavailable');
  phase=result.state==='accepted'?'accepted':result.state==='refused'||result.state==='cancelled'||result.state==='expired'?'refused':'unknown';render();
 }catch(_){if(g===generation&&sheet.open){phase='unknown';render();}}
 finally{if(g===generation){operationController=null;if(!document.hidden&&sheet.open)poll();}}
}
async function status(){
 if(!receipt||operationController)return;const g=generation,p=receipt,controller=new AbortController();operationController=controller;
 try{const result=await request('status',{epoch:p.epoch,intent:p.intent},controller);
  if(g!==generation||!sheet.open)return;
  if(result.intent!==p.intent||result.target?.revision!==p.target.revision||!sameTarget(result.target))throw Error('unavailable');
  phase=result.state==='accepted'?'accepted':result.state==='refused'||result.state==='cancelled'||result.state==='expired'?'refused':'unknown';render();
 }catch(_){if(g===generation&&sheet.open){message='Receipt unavailable. Input was not sent again.';render();}}
 finally{if(g===generation)operationController=null;}
}
function resizeSheet(){
 if(!sheet.open)return;const v=window.visualViewport,h=v?.height||innerHeight,offset=v?.offsetTop||0;
 sheet.style.height=Math.min(746,h-(h<610?8:86))+'px';sheet.style.maxHeight=Math.max(120,h-8)+'px';
 sheet.style.bottom=Math.max(0,innerHeight-h-offset)+'px';sheet.classList.toggle('terminal-keyboard',h<610);
 refreshOutput();
}
window.visualViewport?.addEventListener('resize',resizeSheet);window.visualViewport?.addEventListener('scroll',resizeSheet);window.addEventListener('resize',resizeSheet);
function render(){
 const previous=q('#terminal-output');
 // Review/edit rebuild the sheet for the same pane. Choosing a pane and
 // reopening clear output first, so their new preview still opens at its tail.
 const readTop=previewOK&&previous&&!terminalFollowing(previous)?previous.scrollTop:null;
 const readOutput=readTop!==null?previous.textContent:'',readPreview=readTop!==null?previous._preview:null,readView=readTop!==null?previous._scrollView:null;
 resizeSheet();sheet.replaceChildren();
 const header=make('div','terminal-header'),title=make('h2','',phase==='review'?'Review input':'Terminal');title.id='terminal-title';header.append(title,action('Close','terminal-close',close));sheet.append(header);
 const who=make('div','terminal-identity'),copy=make('div'),portrait=make('img');portrait.src=window.AGENTSTACK_DEMO.portraitURL(name);portrait.alt='';portrait.onerror=()=>portrait.remove();copy.append(make('strong','',name),make('small','','Cockpit session'));who.append(portrait,copy);
 if(choice){const b=make('button','terminal-pane-button',choice.pane_label+' ⌄');b.type='button';b.disabled=['sending','preparing'].includes(phase);b.onclick=list;who.append(b);}
 sheet.append(who);const scroll=make('div','terminal-scroll');sheet.append(scroll);
 if(phase==='loading'){scroll.append(make('p','terminal-help','Finding selectable panes…'));return;}
 if(phase==='choose'){
  scroll.append(make('p','terminal-help','Choose the pane you want to view and send input to.'));
  const paneList=make('div','terminal-choices');for(const c of choices){const b=action('','',()=>choose(c));b.append(make('strong','',c.pane_label),make('small','',c.name));paneList.append(b);}scroll.append(paneList);
  if(message)scroll.append(make('p','terminal-help',message));scroll.append(action('Refresh panes','',list));return;
 }
 if(['unavailable','changed'].includes(phase)){
  scroll.append(make('p','terminal-result',phase==='changed'?'Pane changed':'Terminal unavailable'),make('p','terminal-help',message));sheet.append(action('Refresh panes','terminal-button',list));return;
 }
 const terminal=make('div','terminal-preview'),bar=make('div','terminal-preview-head');bar.append(make('span','terminal-stream',''),make('span','','Terminal tail'));const pre=make('pre','terminal-output');pre.id='terminal-output';terminal.append(bar,pre);scroll.append(terminal);
 pre.addEventListener('scroll',refreshOutput,{passive:true});
 pre.addEventListener('wheel',event=>{
  terminalScrollGesture(pre,event.deltaY>0&&!event.ctrlKey&&!event.metaKey&&!event.shiftKey);
 },{passive:true});
 let touchPoint=null;
 pre.addEventListener('touchstart',event=>{
  const t=event.touches.length===1?event.touches[0]:null;
  touchPoint=t?{id:t.identifier,x:t.clientX,y:t.clientY}:null;
  terminalScrollGesture(pre,false);
 },{passive:true});
 pre.addEventListener('touchmove',event=>{
  const t=event.touches.length===1?event.touches[0]:null;
  if(!t||!touchPoint||t.identifier!==touchPoint.id){touchPoint=null;terminalScrollGesture(pre,false);return;}
  // Finger moving up scrolls toward the tail; horizontal/pinch/upward
  // scrolling must not release a view that only reached bottom by clamping.
  const down=touchPoint.y-t.clientY,across=Math.abs(t.clientX-touchPoint.x);
  touchPoint={id:t.identifier,x:t.clientX,y:t.clientY};
  terminalScrollGesture(pre,down>across);
 },{passive:true});
 pre.addEventListener('touchend',()=>{touchPoint=null;},{passive:true});
 pre.addEventListener('touchcancel',()=>{touchPoint=null;terminalScrollGesture(pre,false);},{passive:true});
 pre.addEventListener('keydown',event=>{
  terminalScrollGesture(pre,!event.defaultPrevented&&(['ArrowDown','PageDown','End'].includes(event.key)||event.key===' '&&!event.shiftKey));
 });
 const observed=make('div','terminal-observed');observed.append(make('span','','Latest 40 lines · up to 6 KiB'),make('span','terminal-age',''));scroll.append(observed,make('p','terminal-preview-note',''));
 if(['editing','preparing'].includes(phase)){
  const label=make('label','terminal-compose-label','Your input');label.htmlFor='terminal-text';const field=make('textarea','terminal-text');field.id='terminal-text';field.rows=2;field.value=text;field.placeholder='Write one line…';field.autocomplete='off';field.spellcheck=false;field.disabled=phase==='preparing';
  const count=make('span','',new TextEncoder().encode(text).length+' / 4096 bytes'),meta=make('div','terminal-input-meta');meta.append(make('span','','One line + Enter'),count);
  scroll.append(label,field,meta,make('p','terminal-help','Check the terminal above before sending.'));
  const actions=make('div','terminal-actions'),review=action(phase==='preparing'?'Preparing…':'Review input','terminal-primary',reviewInput);review.id='terminal-review';review.disabled=phase==='preparing'||!previewOK||!validText(text);
  field.oninput=()=>{text=field.value;count.textContent=new TextEncoder().encode(text).length+' / 4096 bytes';review.disabled=!previewOK||!validText(text);};field.onkeydown=e=>{if(e.key==='Enter')e.preventDefault();};actions.append(action('Cancel','',close),review);sheet.append(actions);
 }else if(phase==='review'){
  scroll.append(make('p','terminal-compose-label','Exact text to '+prepared.target.name),make('pre','terminal-exact',prepared.text));const enter=make('div','terminal-enter');enter.append(make('span','','After this text'),make('kbd','','Enter ↵'));scroll.append(enter,make('p','terminal-help','Sends once to this pane. It may be a shell. Existing terminal input may be combined with it.'));
  const actions=make('div','terminal-actions');actions.append(action('Edit','',()=>{cancelIntent();phase='editing';render();}),action('Send once','terminal-primary',send));sheet.append(actions);
 }else{
  const accepted=phase==='accepted',unknown=phase==='unknown';scroll.append(make('p','terminal-result',accepted?'Received by terminal':unknown?'Outcome unknown':phase==='sending'?'Sending once…':'Input not sent'),make('p','terminal-receipt',accepted?'Input and Enter received. This does not confirm the task is complete.':unknown?'Some input may have arrived. No automatic retry. Check the pane before writing again.':phase==='sending'?'Waiting for the terminal receipt.':'The input was refused or its confirmation expired.'));
  if(accepted)scroll.append(make('p','terminal-result-note','The pane keeps updating while this sheet is open.'));
  const actions=make('div','terminal-actions');actions.append(action('Close','',close));if(unknown)actions.append(action('Check receipt','terminal-primary',status));else if(phase!=='sending')actions.append(action('Write again','terminal-primary',()=>{cancelIntent();phase='editing';text='';render();}));sheet.append(actions);
 }
 scroll.append(make('p','terminal-message',message));
 if(readTop!==null){pre.textContent=readOutput;pre._preview=readPreview;pre._scrollView=readView;pre.scrollTop=readTop;}
 refreshOutput();
}
sheet.addEventListener('cancel',e=>{e.preventDefault();close();});sheet.addEventListener('close',()=>{if(name)close();});
document.addEventListener('visibilitychange',()=>{if(document.hidden)close();});window.addEventListener('pagehide',close);
window.OrreryMobileTerminal={open,close,mail};entries();
const controller=new AbortController();request('',null,controller).then(value=>{if(((value.api===31&&value.supported==='legacy-managed-pane')||(value.api===34&&value.supported==='fresh-managed-pane'))&&value.enabled===true&&value.ack==='tmux-command-accepted'){capability=Object.freeze({...value});enabled=true;const mode=document.querySelector('body>header>span');if(mode)mode.textContent='Terminal enabled';entries();}}).catch(()=>{});
})();
