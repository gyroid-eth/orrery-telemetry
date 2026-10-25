/* Safe snapshot reader. No operator API, body reader, quota refresh or offline cache. */
(function(){
'use strict';
window.AGENTSTACK_DSH_ENABLED=true;
window.AGENTSTACK_DEMO={assetURL:n=>'/assets/'+n+'.svg',portraitURL:n=>'/view/portrait?window='+desiredWindow+'&name='+n};
const $=s=>document.querySelector(s),node=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;};
let snapshot=null,kind='all',agentFilter='',desiredWindow='day',timer=0,active=null,pending=false,receivedAt=0,lastNotices=[];
const minutesAt=position=>Math.max(15,Math.min(43200,Math.round(Math.exp(Math.log(15)+position/1000*Math.log(43200/15))/15)*15));
const duration=seconds=>seconds===null?'All history':seconds<3600?'Last '+Math.round(seconds/60)+' min':seconds<=86400?'Last '+(seconds/3600).toLocaleString('en-GB',{maximumFractionDigits:2})+'h':'Last '+(seconds/86400).toLocaleString('en-GB',{maximumFractionDigits:2})+'d';
window.OrreryMobileWindow={duration};
const network=new window.OrreryMobileNetwork();
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
 const graph=view==='graph';$('#activity').hidden=graph;$('#graph-panel').hidden=!graph;
 document.querySelectorAll('[data-view]').forEach(x=>x.classList.toggle('active',x.dataset.view===view));
 if(graph){network.applyView();network.hot=Math.max(network.hot,120);network.run();}else network.pause();
}
function renderActivity(){
 if(!snapshot)return;
 const events=[...snapshot.timeline.mail.map(x=>({...x,type:'mail',sort:x.event_at})),...snapshot.timeline.states.map(x=>({...x,type:'state',sort:x.observed_at}))].sort((a,b)=>b.sort-a.sort);
 const container=$('#events');container.replaceChildren();
 const messages={work:'Started working',wait:'Now waiting',ask:'Awaiting review',question:'Needs an answer',finished:'Finished',gone:'Stopped',retired:'Retired',idle:'Idle'};
 for(const e of events){
  if(kind!=='all'&&e.type!==kind)continue;
  if(agentFilter&&![e.source,e.target,e.name].includes(agentFilter))continue;
  const article=node('article','event'+(e.to==='ask'||e.to==='question'?' review':''));
  const stamp=node('time','',time(e.sort)+(e.type==='state'?' · observed':''));article.appendChild(stamp);
  if(e.type==='mail'){
   const route=node('div','mail-route');route.appendChild(person(e.source));const to=person(e.target);to.classList.add('route-to');route.appendChild(to);route.appendChild(node('span','route-kind',e.kind==='cc'?'Cc recipient':'To recipient'));article.appendChild(route);
  }else{
   const row=node('div','status-line'+(e.to==='ask'||e.to==='question'?' review':''));const who=snapshot.agents.find(a=>a.name===e.name)||{name:e.name};row.appendChild(face(who));const copy=node('div');copy.append(node('div','name',e.name),node('p','',messages[e.to]||'Status changed'));row.appendChild(copy);article.appendChild(row);
  }
  container.appendChild(article);
 }
 if(!container.children.length)container.appendChild(node('p','empty',snapshot.sources.mail.status==='unavailable'?'Mail observations unavailable':'No matching events observed'));
 const notes=['Mail shows to/cc recipient events. Status changes were observed after this reader started.'];
 if(snapshot.timeline.events_truncated)notes.push('Only the latest 200 Mail events are shown.');
 if(snapshot.timeline.edges_truncated)notes.push('Graph is limited to 10,000 Mail links; period counts are incomplete.');
 else if(snapshot.timeline.events_truncated)notes.push('Graph counts cover the full selected period.');
 if(snapshot.timeline.states_truncated)notes.push('Only the latest 200 observed status changes are retained.');
 if(snapshot.sources.mail.status==='stale')notes.push('Mail observations are stale.');
 $('#timeline-note').textContent=notes.join(' ');
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
 clearTimeout(timer);if(document.hidden){active?.abort();network.pause();}else{poll();network.run();}
});
// Browser clocks can differ from the host; age advances from the reported
// observation age plus local elapsed time, not by pretending clocks agree.
setInterval(()=>{
 if(!snapshot||document.hidden)return;
 const elapsed=(performance.now()-receivedAt)/1000;
 if(Object.values(snapshot.sources).some(s=>s.age_seconds!==null&&s.age_seconds+elapsed>30)){
  markStale('Observations are stale. Showing the last snapshot.');
 }
},5000);
poll();
})();
