/* View-only reader: metadata lists, explicit single-message disclosure, no offline bodies. */
(function(){
'use strict';
if(location.pathname==='/reader-pane'){
 const parentView=window.OrreryMobilePane?.parent;
 if(parentView){
  for(const key of ['AGENTSTACK_DEMO','OrreryMobileAgent','OrreryMobileIdentity','OrreryMobileProfile','OrreryMobileWindow'])window[key]=parentView[key];
 }
 return;
}
window.AGENTSTACK_DSH_ENABLED=true;
window.AGENTSTACK_DEMO={assetURL:n=>'/assets/'+n+'.svg',portraitURL:n=>'/view/portrait?window='+desiredWindow+'&name='+n};
const $=s=>document.querySelector(s),node=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;};
function icon(name){
 const namespace='http://www.w3.org/2000/svg';
 const svg=document.createElementNS(namespace,'svg'),use=document.createElementNS(namespace,'use');
 svg.setAttribute('class','mobile-icon');svg.setAttribute('aria-hidden','true');use.setAttribute('href','#icon-'+name);svg.append(use);
 return svg;
}
const tabletView=matchMedia('(min-width: 740px) and (min-height: 540px)');
let readingView='graph',tabletLayout=tabletView.matches;
let snapshot=null,kind='all',agentFilter='',desiredWindow='day',timer=0,active=null,pending=false,receivedAt=0,lastNotices=[];
const mailRef=a=>a&&/^[a-f0-9]{32}$/.test(a.mail_instance_id)&&Number.isSafeInteger(a.mail_agent_id)&&a.mail_agent_id>0?{mail_instance_id:a.mail_instance_id,mail_agent_id:a.mail_agent_id}:null;
function markRef(element,value){if(typeof value?.name==='string')element.dataset.agentName=value.name;const ref=mailRef(value);if(ref){element.dataset.mailInstanceId=ref.mail_instance_id;element.dataset.mailAgentId=String(ref.mail_agent_id);}}
window.OrreryMobileAgent={forName(name){const rows=(snapshot?.agents||[]).filter(a=>a.name===name);return rows.length===1?{state:rows[0].act_state,running:rows[0].running,workDisplay:typeof rows[0].work_disp==='string'?rows[0].work_disp:'',stale:snapshot.sources.agents.status!=='available'||snapshot.sources.agents.age_seconds===null||snapshot.sources.agents.age_seconds+(performance.now()-receivedAt)/1000>30}:null;}};
window.OrreryMobileIdentity={forName(name){const rows=(snapshot?.agents||[]).filter(a=>a.name===name);return rows.length===1?mailRef(rows[0]):null;},roster(){const source=snapshot?.sources.agents;return source?.status==='available'&&source.age_seconds!==null&&source.age_seconds+(performance.now()-receivedAt)/1000<=30?snapshot.agents:null;}};
let mailPage=null,mailPositions=[null],mailPosition=0,mailController=null,mailGeneration=0;
let pair=null,pairController=null,pairGeneration=0,detailController=null,detailGeneration=0;
const minutesAt=position=>Math.max(15,Math.min(43200,Math.round(Math.exp(Math.log(15)+position/1000*Math.log(43200/15))/15)*15));
const duration=seconds=>seconds===null?'All history':seconds<3600?'Last '+Math.round(seconds/60)+' min':seconds<=86400?'Last '+(seconds/3600).toLocaleString('en-GB',{maximumFractionDigits:2})+'h':'Last '+(seconds/86400).toLocaleString('en-GB',{maximumFractionDigits:2})+'d';
window.OrreryMobileWindow={duration};
const network=new window.OrreryMobileNetwork(null,(a,b)=>openPair(a,b),openGraphAgent);
window.OrreryMobileGraph={
 agents:()=>snapshot?.agents||[],
 show(){graphFullscreen?.exit();setGraphSearch(false);if(!$('#settings').hidden)settings(false);showView('graph');},
 pickWith(callback){network.pickNode=callback;},
 resize(){if(network.svg.clientHeight&&network.svg.clientWidth){if(network.userView)network.applyView();else network.fit();}},
};
function openGraphAgent(name,returnFocus){
 const terminal=window.OrreryMobileTerminal,status=$('#graph-reader-status');
 status.hidden=terminal?.available()===true;
 if(!status.hidden){status.textContent='Reader is unavailable. Check Phone access on the desktop.';return;}
 terminal.open(name,window.OrreryMobileIdentity?.forName(name),null,closedName=>{
  network.focus(closedName);
  if(network.svg.contains(document.activeElement))document.activeElement.blur();
  returnFocus?.focus({preventScroll:true});
 });
}
let graphFullscreen=null;
const time=ts=>typeof ts==='number'?new Date(ts*1000).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}):'Unknown';
function face(a,initialLength=2){
 const wrap=node('span','face'),img=node('img');img.src=window.AGENTSTACK_DEMO.portraitURL(a.name);img.alt='';
 img.onerror=()=>{img.remove();wrap.prepend(node('span','initials',a.name.slice(0,initialLength)));};wrap.appendChild(img);
 const badge=providerMark(a);if(badge)wrap.appendChild(badge);
 return wrap;
}
function providerMark(a,size=18){
 const provider=window.OrreryNetworkNode.providerKeyFor(a);if(!provider)return null;
 const badge=window.OrreryNetworkNode.providerBadge(provider,0,0,size);if(!badge)return null;
 const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('class','badge node');svg.setAttribute('viewBox','-10 -10 20 20');svg.setAttribute('aria-hidden','true');svg.append(badge);return svg;
}
function readerProfile(name,recordedModel=''){
 const rows=(snapshot?.agents||[]).filter(a=>a.name===name);if(rows.length!==1)return null;
 const a=rows[0],profile={name:a.name,model:a.model||'',provider:a.provider||'',locality:a.locality};
 if(recordedModel){
  profile.model=recordedModel;
  if(profile.locality!=='local'&&profile.provider!=='local')profile.provider=/^deepseek[- /]/i.test(recordedModel)?'deepseek':'';
 }
 return profile;
}
window.OrreryMobileProfile={forName:readerProfile,face(name,model){const e=face(readerProfile(name,model)||{name},1);e.setAttribute('aria-hidden','true');return e;}};
function person(name){const a=snapshot.agents.find(x=>x.name===name)||{name,provider:'',model:'',locality:null};const e=node('div','person');e.append(face(a),node('span','name',name));return e;}
function syncView(){
 const graphPanel=$('#graph-panel'),wasHidden=graphPanel.hidden;
 const split=tabletView.matches,showGraph=split||readingView==='graph';
 tabletLayout=split;
 const activity=$('#activity'),controls=$('.activity-head'),controlsParent=split?$('.sidebar-head'):activity;
 $('#mobile-main').dataset.mobileView=readingView;
 activity.hidden=!split&&readingView==='graph';
 if(controls.parentElement!==controlsParent){
  const focus=document.activeElement;
  if(split)controlsParent.append(controls);
  else controlsParent.prepend(controls);
  if(controls.contains(focus)&&!activity.hidden)focus.focus({preventScroll:true});
 }
 graphPanel.hidden=!showGraph;
 $('[data-view="activity"]').hidden=split||readingView==='activity';
 $('[data-view="graph"]').hidden=split||readingView==='graph';
 if(!showGraph){
  graphFullscreen?.exit();
  setGraphSearch(false);
  if(!$('#settings').hidden)settings(false);
  closeGraphMenus();
  network.pause();
 }else if(wasHidden){
  network.applyView();network.hot=Math.max(network.hot,120);network.run();
 }
}
function showView(view){
 clearDetail();clearPair();
 readingView=view;syncView();
}
function setGraphSearch(open){
 const search=$('#graph-search'),toggle=$('#graph-search-toggle');
 search.hidden=!open;toggle.setAttribute('aria-expanded',String(open));
 if(open)$('#agent-search').focus({preventScroll:true});
}
const stateIcons={work:'working',wait:'waiting',ask:'review',question:'review',finished:'finished',gone:'stopped',retired:'retired',idle:'idle'};

function renderActivity(){
 if(!snapshot)return;
 const mail=mailPage?.events||snapshot.timeline.mail;
 const events=[...mail.map(x=>({...x,type:'mail',sort:x.event_at})),...(mailPage?[]:snapshot.timeline.states).map(x=>({...x,type:'state',sort:x.observed_at}))].sort((a,b)=>b.sort-a.sort);
 const container=$('#events');container.replaceChildren();
 const messages={work:'Started working',wait:'Now waiting',ask:'Awaiting review',question:'Needs an answer',finished:'Finished',gone:'Stopped',retired:'Retired',idle:'Idle'};
 let previousMailMinute=null;
 for(const e of events){
  if(kind!=='all'&&e.type!==kind)continue;
  if(agentFilter&&![e.source,e.target,e.name].includes(agentFilter))continue;
  const article=node('article','event'+(e.to==='ask'||e.to==='question'?' review':''));
  const stamp=node('time','',time(e.sort));
  stamp.title=e.type==='state'?'Observed at '+stamp.textContent:stamp.textContent;
  if(e.type==='mail'){
   const minute=Number.isFinite(e.sort)?Math.floor(e.sort/60):null;
   stamp.classList.toggle('repeat-minute',minute!==null&&minute===previousMailMinute);
   previousMailMinute=minute;
   article.appendChild(mailButton(e,mailPage?.window||snapshot.window,stamp));
  }else{
   previousMailMinute=null;
   article.appendChild(stamp);
   const row=node('div','status-line'+(e.to==='ask'||e.to==='question'?' review':''));
   markRef(row,e);
   const status=messages[e.to]||'Status changed',who=snapshot.agents.find(a=>a.name===e.name)||{name:e.name};
   const state=node('span','event-state');state.title=status;
   state.append(icon(stateIcons[e.to]||'info'),node('span','sr-only',status));
   row.append(face(who),node('span','name',e.name),state);article.appendChild(row);
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
 const cacheNote=$('#mail-cache-note');
 cacheNote.hidden=!cache;
 cacheNote.textContent=cache?'Mac 24h cache: '+cache.status+' · '+cache.messages+' messages. Cache coverage does not limit older message viewing.':'';
 $('#agent-filter').hidden=!agentFilter;$('#agent-filter').textContent=agentFilter+' ×';
}
function showNotices(notices,data,label){
 lastNotices=notices;

 const sources=data?.sources,unknown=data?.period?.activity_unknown;
 if(!label){
  if(sources&&[sources.agents,sources.graph].some(s=>s.status==='unavailable'))label='Unavailable';
  else if(sources?.mail.status==='unavailable')label='Mail unavailable';
  else if(sources&&Object.values(sources).some(s=>s.status==='stale'))label='Stale';
  else if(unknown)label=unknown+(data.period.seconds===null?' unknown':' excluded');
  else label='Observation notes';
 }
 for(const [id,name] of [['observation-notice','Graph'],['activity-notes','Activity']]){
  const notice=$('#'+id),summary=notice.querySelector('summary');
  summary.title=name+' details'+(notices.length?': '+label:'');
  summary.setAttribute('aria-label',summary.title);
  notice.classList.toggle('has-warning',Boolean(notices.length));
 }
 const healthLabel=$('#health-label'),warning=notices.length?label:'';
 if(healthLabel.textContent!==warning)healthLabel.textContent=warning;
 for(const id of ['health','activity-health']){
  const health=$('#'+id);
  health.textContent=notices.join(' ');
  health.hidden=!notices.length;
 }
 $('#snapshot-period-note').textContent=data?.period?'Snapshot period: '+duration(data.period.seconds)+'.':'No snapshot has been observed yet.';
}
const attentionReasons={ask:'Awaiting review',question:'Needs an answer'};
const needsReview=a=>Object.hasOwn(attentionReasons,a.act_state);
const statusAgentMenus=[...document.querySelectorAll('[data-agent-status]')].map(details=>{
 const state=details.dataset.agentStatus;
 return {
  details, trigger:details.querySelector('summary'), menu:details.querySelector('.graph-status-agents'),
  count:details.querySelector('b'), state, signature:'',
  label:state==='work'?'Working':'Waiting',
 };
});
const graphStatusMenu=$('#graph-panel .graph-status'), observationMenu=$('#observation-notice');
const graphMenus=[...statusAgentMenus.map(group=>group.details),observationMenu];
function closeGraphMenus(keep=null){
 for(const details of graphMenus)if(details!==keep&&details.open)details.open=false;
}
for(const details of graphMenus)details.addEventListener('toggle',()=>{
 if(details.open)closeGraphMenus(details);
});
document.addEventListener('pointerdown',event=>{
 if(!graphStatusMenu.contains(event.target))closeGraphMenus();
});
graphStatusMenu.addEventListener('keydown',event=>{
 if(event.key!=='Escape')return;
 const open=graphMenus.find(details=>details.open);
 if(open){event.preventDefault();closeGraphMenus();open.querySelector('summary').focus({preventScroll:true});}
});
new ResizeObserver(()=>{
 const net=$('#net').getBoundingClientRect(),position=graphStatusMenu.getBoundingClientRect();
 graphStatusMenu.style.setProperty('--graph-agent-list-width',Math.max(0,net.right-position.left-10)+'px');
 graphStatusMenu.style.setProperty('--graph-agent-list-height',Math.max(0,net.bottom-position.bottom-10)+'px');
}).observe($('#net'));

function renderGraphCounts(data,observed,fresh){
 const summary=$('#summary'), labels=[];
 const running=observed?data.agents.filter(a=>a.running):[];
 for(const group of statusAgentMenus){
  const {state,label}=group;
  const active=running.filter(a=>a.act_state===state);
  const attention=state==='work'?running.filter(needsReview):[];
  const agents=[...attention,...active];
  const labelWithCount=label+': '+active.length;
  const note=attention.length?', need attention: '+attention.length:'';
  labels.push(labelWithCount+note);
  group.count.textContent=observed?String(active.length):'–';
  group.details.classList.toggle('has-attention',attention.length>0);
  group.trigger.title=label+': '+(observed?(fresh?'':'last known ')+active.length+note:'unavailable');
  group.trigger.setAttribute('aria-label',group.trigger.title);
  const signature=JSON.stringify([observed,agents.map(a=>[a.name,a.provider,a.model,a.locality,a.act_state])]);
  if(group.signature===signature)continue;
  group.signature=signature;
  const focus=group.menu.contains(document.activeElement)?document.activeElement.dataset.name:'';
  group.menu.replaceChildren();
  for(const a of agents){
   const button=node('button','graph-status-agent');button.type='button';button.dataset.name=a.name;
   const portrait=face(a);portrait.setAttribute('aria-hidden','true');
   button.append(portrait,node('span','graph-status-name',a.name));
   if(needsReview(a)){
    button.title=attentionReasons[a.act_state];
    button.setAttribute('aria-label',a.name+' · '+button.title);
    const flag=node('span','graph-status-flag '+a.act_state,a.act_state==='ask'?'!':'?');
    flag.setAttribute('aria-hidden','true');
    button.append(flag);
   }
   button.onclick=()=>{closeGraphMenus();group.trigger.focus({preventScroll:true});openGraphAgent(a.name,group.trigger);};
   group.menu.append(button);
  }
  if(!agents.length)group.menu.append(node('p','',observed?'No '+label.toLowerCase()+' agents':'Agent observations unavailable'));
  if(focus)(Array.from(group.menu.children).find(item=>item.dataset.name===focus)||group.trigger).focus({preventScroll:true});
 }
 const label=observed?(fresh?'':'Last known: ')+labels.join(', '):'Agent counts unavailable';
 const status=$('#graph-count-label');
 if(status.textContent!==label)status.textContent=label;
 summary.classList.toggle('is-stale',observed&&!fresh);
}
function render(data){
 if(snapshot?.timeline.scope&&snapshot.timeline.scope!==data.timeline.scope){clearDetail();clearPair();resetMail();}
 snapshot=data;receivedAt=performance.now();
 window.OrreryMobileTerminal?.restore?.();
 // The displayed roster is the projection's graph.nodes + agents union.
 // Mail success cannot establish an empty roster or agent status counts.
 const rosterSources=[data.sources.agents,data.sources.graph],mail=data.sources.mail;
 const rosterObserved=rosterSources.some(s=>s.observed_at!==null&&s.status!=='unavailable');
 const rosterFresh=rosterSources.every(s=>s.status==='available');
 renderGraphCounts(data,rosterObserved,rosterFresh);
 $('#observed').textContent='Observed '+time(data.observed_at);
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
 const needs=data.agents.filter(a=>needsReview(a)||a.state==='unknown');
 const attentionCount=$('#attention-count');
 attentionCount.querySelector('b').textContent=rosterObserved?String(needs.length):'–';
 attentionCount.title=rosterObserved?'Needs attention: '+(rosterFresh?'':'last known ')+needs.length:'Attention count unavailable';
 attentionCount.querySelector('.sr-only').textContent=attentionCount.title;
 for(const a of needs.slice(0,3)){
  const review=needsReview(a);
  const row=node('button','attention-row '+(review?'review':'unknown'));row.type='button';
  markRef(row,a);
  const reason=review?attentionReasons[a.act_state]:a.last_active?'Unknown · last active '+time(a.last_active):'Observation unavailable';
  row.title=reason;row.setAttribute('aria-label',a.name+' · '+reason);
  const status=node('span','attention-status');status.append(icon(review?'review':'unknown'));
  row.append(face(a),node('span','name',a.name),status);
  row.onclick=()=>{
   agentFilter=a.name;kind='all';syncFilters();showView('activity');renderActivity();
   $('#activity').scrollTop=0;$('#agent-filter').focus({preventScroll:true});
  };
  attention.append(row);
 }
 if(needs.length>3){
  const more=node('span','attention-more','+'+(needs.length-3));
  more.title=(needs.length-3)+' more agents need attention';
  attention.append(more);
 }
 renderActivity();network.update(data);
 window.OrreryMobileWorkspace?.refresh();
 window.OrreryMobileConversation?.refreshProfiles?.();
 window.OrreryMobileConversation?.refreshActivity?.();
 $('#snapshot-note').textContent='Snapshot observed '+time(data.observed_at)+'. Account usage unavailable. Context arcs are separate.';
}
function syncFilters(){
 document.querySelectorAll('[data-kind]').forEach(b=>{
  const selected=b.dataset.kind===kind;
  b.classList.toggle('active',selected);b.setAttribute('aria-pressed',String(selected));
 });
}
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
tabletView.addEventListener('change',syncView);
window.addEventListener('resize',()=>{if(tabletView.matches!==tabletLayout)syncView();});
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{
 showView(b.dataset.view);
 $('[data-view]:not([hidden])')?.focus({preventScroll:true});
});
$('#graph-search-toggle').onclick=()=>setGraphSearch($('#graph-search').hidden);
$('#graph-search').addEventListener('keydown',event=>{
 if(event.key==='Escape'){
  setGraphSearch(false);$('#graph-search-toggle').focus({preventScroll:true});
 }
});
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
 periodButtons.forEach(b=>{
  b.querySelector('span').textContent=text==='All history'?'∞':text.replace('Last ','').replace(' min','m');
  const label='Change time range: '+text;
  b.title=label+'. Applies to Graph and Activity.';b.setAttribute('aria-label',label);
 });
}
range.value=Math.round(Math.log(1440/15)/Math.log(43200/15)*1000);
range.setAttribute('aria-valuetext',duration(86400));
window.OrrerySpawnControls?.mountScrubber(range, {
 stops:[0,Number(range.value),1000],
 stopLabels:['15 min','24h','30 days'],
 formatValue:value=>duration(minutesAt(value)*60),
});
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

function mailButton(event,period,stamp=null){
 const route=node('button','mail-route'+(stamp?' activity-mail':''));route.type='button';route.dataset.event=event.key;
 const from=person(event.source),to=person(event.target),recipient=event.kind==='cc'?'Cc':'To';
 to.classList.add('route-to');
 if(stamp){
  route.title=event.source+' → '+event.target+' · '+recipient;
  route.setAttribute('aria-label','Read Mail from '+event.source+' · '+recipient+' '+event.target+' · '+stamp.textContent);
  const forward=icon('forward');forward.classList.add('mail-forward');
  from.append(forward);to.prepend(icon('recipient'));
  route.append(from,stamp,to,icon('mail'));
 }else{
  route.append(from,to,node('span','route-kind',recipient+' recipient'),node('span','open-message','Read message ›'));
 }
 route.onclick=()=>openMessage(event,period);return route;
}
function clearDetail(close=true){
 document.querySelector('.terminal-mail-people')?.remove();
 detailGeneration++;detailController?.abort();detailController=null;
 for(const id of ['message-route','message-status','message-subject','message-body'])$('#'+id).textContent='';
 $('#message-title').textContent='Message';
 if(close&&$('#message-sheet').open)$('#message-sheet').close();
}
async function openMessage(event,period){
 clearDetail(false);const generation=detailGeneration,controller=new AbortController();detailController=controller;
 const sheet=$('#message-sheet');if(!sheet.open)sheet.showModal();
 $('#message-route').textContent=event.source+' → '+event.target+' · '+time(event.event_at)+' · '+event.kind;
 globalThis.OrreryMobileTerminal?.mail(event);
 $('#message-status').textContent='Loading one message…';
 const timeout=setTimeout(()=>controller.abort(),8000);
 try{
  const response=await fetch('/view/message?'+new URLSearchParams({window:period,event:event.key}),{signal:controller.signal,cache:'no-store',credentials:'same-origin',referrerPolicy:'no-referrer'});
  const data=await response.json();
  if(generation!==detailGeneration||document.hidden||!sheet.open)return;
  if(!response.ok||!data.event||['key','source','target','kind','event_at'].some(k=>data.event[k]!==event[k])||typeof data.subject!=='string'||typeof data.body!=='string')throw Error('unavailable');
  $('#message-title').textContent='Message';$('#message-subject').textContent=data.subject||'(No subject)';
  const body=$('#message-body'),markdown=window.OrreryConversationMarkdown;
  if(markdown)markdown.renderDocument(data.body,body,{codeControls:true});
  else body.textContent=data.body;
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
 const notice=$('#observation-notice');
 const buttonIcon=button.querySelector('use');
 function setExpanded(expanded){
  const label=expanded?'Close expanded graph':'Expand graph';
  button.title=label;button.setAttribute('aria-label',label);button.setAttribute('aria-expanded',String(expanded));
  buttonIcon.setAttribute('href',expanded?'#icon-collapse':'#icon-expand');
 }
 let active=false,nativeOwned=false,generation=0,sequence=0,entry=0,scroll=null;
 let oldRestoration=null,size=network.dimensions();
 const ownHistory=()=>entry&&history.state?.orreryGraphFullscreen===entry;
 const closeTop=()=>{
  const dialogs=[...document.querySelectorAll('dialog[open]')],top=dialogs.at(-1);
  if(top){if(top.id==='terminal-sheet')window.OrreryMobileTerminal.close();else top.close();return true;}
  if(!$('#settings').hidden){settings(false);return true;}
  if(graphMenus.some(details=>details.open)){closeGraphMenus();return true;}
  if(!$('#graph-search').hidden){setGraphSearch(false);$('#graph-search-toggle').focus({preventScroll:true});return true;}
  return false;
 };
 const resize=()=>{
  if($('#graph-panel').hidden)return;
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
  notice.open=false;setExpanded(false);
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
  setExpanded(true);
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
syncView();
poll();
})();

/* Common, opt-in pane terminal. No preview/draft/intent enters storage or URLs.
   Only the agent, tab and available Mail identity IDs enter this launch's sessionStorage. */
(()=>{'use strict';
const pane=window.OrreryMobilePane;
if(window.location?.pathname==='/reader-pane'&&!pane)return;
const q=s=>document.querySelector(s),make=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!==undefined)e.textContent=txt;return e;};
const sheet=q('#terminal-sheet'),contract='orrery-mobile-pane-v1';
let capability=null,enabled=false,phase='loading',name='',choices=[],choice=null,target=null,text='',output='',preview=null,previewOK=false,message='';
let selector=null,history=false,tab='terminal',composer=false,backEntry=false,backClosing=false,inputObserver=null;
const conversation=()=>window.OrreryMobileConversation;
const readerMotion=window.OrreryReaderSendMotion;
const sendFeedback=readerMotion?.createSendFeedback();
const busy=()=>['sending','preparing'].includes(phase);
const fresh=()=>capability?.api===34;
const ref=value=>value&&/^[a-f0-9]{32}$/.test(value.mail_instance_id)&&Number.isSafeInteger(value.mail_agent_id)&&value.mail_agent_id>0?{mail_instance_id:value.mail_instance_id,mail_agent_id:value.mail_agent_id}:null;
const rowRef=row=>ref({mail_instance_id:row.dataset.mailInstanceId,mail_agent_id:Number(row.dataset.mailAgentId)});
const sameTarget=value=>!fresh()||!!ref(value)&&!!selector&&value.mail_instance_id===selector.mail_instance_id&&value.mail_agent_id===selector.mail_agent_id;
let generation=0,previewController=null,operationController=null,pollTimer=0,expiryTimer=0,receiptTimer=0,failures=0,prepared=null,receipt=null,previousFocus=null,afterClose=null;
let replyReference=null,preparedReference=null;
const referenceTarget=()=>target?JSON.stringify([name,capability?.epoch,target.choice,target.revision,selector?.mail_instance_id,selector?.mail_agent_id]):'';
const referenceCurrent=value=>!value||value.target===referenceTarget()&&value.value.isCurrent();
const referenceError=()=>replyReference&&!referenceCurrent(replyReference)?'Target or conversation changed. Remove this reference and select again.':'';
function addReference(value){
 if(phase!=='editing'||!previewOK||!target||value.agent!==name||!value.isCurrent())return false;
 replyReference={value:{...value,text:value.text.replace(/\s+/gu,' ').trim()},target:referenceTarget()};
 render();window.OrreryReaderReplyMotion?.confirmReference?.(sheet.querySelector('.reader-reference-card'));
 q('#terminal-text')?.focus({preventScroll:true});return true;
}
function refreshReference(){
 if(replyReference&&phase==='review'&&!referenceCurrent(preparedReference)){cancelIntent();phase='editing';message='';render();return;}
 q('#terminal-input-error')?.refresh?.();
 const review=q('#terminal-review');if(review&&replyReference)review.disabled=phase!=='editing'||!previewOK||!!attachmentController||!inputValid();
}
const MAX_ATTACHMENTS=10;
const imageTypes=['image/jpeg','image/png','image/webp'], imageExtensions=['jpg','jpeg','png','webp'];
const fileFormats=['pdf','txt','md','csv','json'];
let attachmentAccess={image:false,file:false},attachmentProbe='',attachmentController=null,attachmentProbeController=null,attachments=[],pendingAttachmentSelection=null;
let paused=false,checkingView=false,resumeController=null,reviewUntil=0,startupReady=false;
const viewKey='orrery-mobile-view-v1';
function savedView(){try{const v=JSON.parse(window.sessionStorage.getItem(viewKey));return v&&(Object.keys(v).length===2||Object.keys(v).length===4&&ref(v))&&Object.keys(v).every(k=>['agent','tab','mail_instance_id','mail_agent_id'].includes(k))&&/^[A-Za-z][A-Za-z0-9_-]{0,95}$/.test(v.agent)&&['conversation','terminal'].includes(v.tab)?v:null;}catch(_){return null;}}
let restoreView=pane?null:savedView();
function rememberView(){if(pane||!sheet.open||!name)return;const identity=ref(selector);try{window.sessionStorage.setItem(viewKey,JSON.stringify({agent:name,tab,...identity}));}catch(_){}}
function forgetView(){if(pane)return;try{window.sessionStorage.removeItem(viewKey);}catch(_){}}
function restoreSavedView(){
 if(pane||!startupReady||document.hidden||!(enabled||history))return;
 const notification=/^#notify=([A-Za-z][A-Za-z0-9_-]{0,95})$/.exec(window.location?.hash||'');
 if(notification){
  const rows=window.OrreryMobileIdentity?.roster();if(!rows)return;
  const found=rows.filter(a=>a.name===notification[1]&&!a.retired);
  if(found.length!==1){const status=q('#graph-reader-status');status.hidden=false;status.textContent='That agent is no longer available.';return;}
  const identity=ref(found[0]);if(fresh()&&!identity)return;
  open(found[0].name,identity);return;
 }
 if(!restoreView||sheet.open)return;
 const identity=window.OrreryMobileIdentity?.forName(restoreView.agent);
 const saved=ref(restoreView);
 if(saved){
  // A display name can be reassigned. Never resume another Mail identity.
  if(!identity)return;
  if(identity.mail_instance_id!==saved.mail_instance_id||identity.mail_agent_id!==saved.mail_agent_id){restoreView=null;forgetView();return;}
 }
 const view=restoreView;restoreView=null;open(view.agent,identity,view.tab);
}
const action=(label,cls,fn)=>{const b=make('button','terminal-button '+cls,label);b.type='button';b.onclick=fn;return b;};
async function request(operation,payload,controller,timeout=3000){
 const timer=setTimeout(()=>controller.abort(),timeout);
 try{
  const response=await fetch('/view/terminal'+(operation?'/'+operation:''),{method:operation?'POST':'GET',
   headers:operation?{'Content-Type':'application/json'}:{},body:operation?JSON.stringify(payload):undefined,
   credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',signal:controller.signal});
  const raw=await response.text();if(new TextEncoder().encode(raw).length>16384)throw Error('unavailable');
  const value=JSON.parse(raw);
  if(!response.ok||value.contract!==contract||operation&&value.epoch!==capability.epoch)throw Error((operation==='image'&&/^image_[a-z_]+$/.test(value.error)||operation==='file'&&/^file_[a-z_]+$/.test(value.error))?value.error:'unavailable');
  return value;
 }finally{clearTimeout(timer);}
}
function cancelIntent(){
 preparedReference=null;
 clearTimeout(receiptTimer);
 const old=prepared||receipt;prepared=null;receipt=null;reviewUntil=0;clearTimeout(expiryTimer);
 if(!old||!enabled)return;
 const controller=new AbortController();
 // Best effort cancellation never retries commit, and keeps no text or preview.
 request('cancel',{epoch:old.epoch,intent:old.intent},controller,2000).catch(()=>{});
}
function stop(){clearTimeout(pollTimer);pollTimer=0;previewController?.abort();previewController=null;operationController?.abort();operationController=null;resumeController?.abort();resumeController=null;attachmentController?.abort();attachmentController=null;attachmentProbeController?.abort();attachmentProbeController=null;attachmentAccess={image:false,file:false};attachmentProbe='';pendingAttachmentSelection=null;clearAttachments();paused=false;checkingView=false;generation++;}
function clear(){window.OrreryMobilePush?.close();sendFeedback?.reset();replyReference=preparedReference=null;inputObserver?.disconnect();inputObserver=null;readerSwipe=null;composer=false;stop();cancelIntent();conversation()?.unmount();tab='terminal';name='';selector=null;choices=[];choice=null;target=null;text='';output='';preview=null;previewOK=false;message='';phase='loading';failures=0;sheet.replaceChildren();}
function close(fromBack=false,keepView=false){
 if(pane&&fromBack!==true){pane.close();return;}
 if(!keepView){restoreView=null;forgetView();}
 const remove=backEntry&&window.history.state?.orreryConversation===true,closedName=name,finished=afterClose,restore=previousFocus;
 backEntry=false;previousFocus=afterClose=null;clear();if(sheet.open)sheet.close();
 if(!document.hidden&&!keepView&&finished)finished(closedName);
 else if(!document.hidden&&restore?.isConnected)restore.focus({preventScroll:true});
 if(remove&&fromBack!==true){backClosing=true;window.history.back();}
}
window.addEventListener('popstate',event=>{if(backClosing){backClosing=false;event.stopImmediatePropagation();return;}if(sheet.open&&backEntry){event.stopImmediatePropagation();if(dismissLayer()){window.history.pushState({orreryConversation:true},'');return;}backEntry=false;close(true);}},true);
function updateEntry(b){
 const label=history?'History':'Terminal';
 b.disabled=!(enabled||history);
 const actionLabel='Open '+label+' for '+b.dataset.terminalName;
 b.title=b.disabled?'Reader unavailable':actionLabel;
 b.setAttribute('aria-label',actionLabel);
 if(b.dataset.entryKind===label)return;
 b.dataset.entryKind=label;
 const symbol=history?'history':'terminal';
 b.innerHTML='<svg class="mobile-icon" aria-hidden="true"><use href="#icon-'+symbol+'"></use></svg>';
}
function entry(n,cls='',identity=null){
 const b=make('button','terminal-entry icon-button '+cls);b.type='button';b.dataset.terminalName=n;
 updateEntry(b);
 b.onclick=()=>open(n,identity);return b;
}
function entries(){
 if(pane)return;
 q('#attention').querySelectorAll(':scope > .attention-row').forEach(row=>{const n=row.dataset.agentName;if(!n)return;const wrap=make('div','terminal-attention');row.replaceWith(wrap);wrap.append(row,entry(n,'',rowRef(row)));});
 q('#events').querySelectorAll('.status-line').forEach(row=>{const n=row.dataset.agentName;if(n&&!row.querySelector('.terminal-entry'))row.append(entry(n,'',rowRef(row)));});
 document.querySelectorAll('.terminal-entry:not(#terminal-graph-entry)').forEach(updateEntry);
 restoreSavedView();
}
new MutationObserver(entries).observe(q('#attention'),{childList:true});new MutationObserver(entries).observe(q('#events'),{childList:true});
function mail(event){
 q('.terminal-mail-people')?.remove();const people=make('div','terminal-mail-people');
 for(const [label,n,id]of[['From',event.source,event.source_agent_id],['To',event.target,event.target_agent_id]]){const row=make('div','terminal-mail-person');row.append(make('span','',label+' '+n),entry(n,'',ref({mail_instance_id:event.mail_instance_id,mail_agent_id:id})));people.append(row);}
 q('#message-route').after(people);
}
async function open(n,identity=null,initialTab=null,onClose=null){
 if(busy()||sheet.open&&phase==='unknown'||!(enabled||history)||document.hidden)return;
 if(pane&&n!==pane.name){pane.openPeer(n,identity);return;}
 const opening=!sheet.open;
 // Keep the entry route while navigating to another agent within this sheet.
 if(opening){previousFocus=document.activeElement;afterClose=onClose;}
 clear();name=n;selector=ref(identity);window.scrollTo({top:0,behavior:'instant'});
 if(opening)sheet.showModal();
 // Consume only after the Reader accepts the target, before adding its Back
 // entry so Back cannot resurrect the same notification link.
 if(window.location?.hash==='#notify='+n)window.history.replaceState(window.history.state,'',window.location.pathname+window.location.search);
 if(opening&&history&&!pane){window.history.pushState({orreryConversation:true},'');backEntry=true;}
 if(history){tab=initialTab==='terminal'&&enabled?'terminal':'conversation';conversation().mount(n);}
 if(!enabled){phase='history-only';render();return;}
 if(fresh()&&!selector){phase='unavailable';message='A unique Mail identity is required. Refresh the agent list or open a Mail participant.';render();return;}
 render();await list();
}
async function list(){
 if(busy()||phase==='unknown')return;
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
function switchTab(value){if(busy()||value===tab||!history||!enabled&&value==='terminal')return;tab=value;render();}
// Tab swipes belong only to reading surfaces; native scroll and selection win.
let readerSwipe=null;
function swipeAvailable(){
 return sheet.open&&history&&enabled&&!composer&&!text&&!attachmentController&&!busy()&&phase!=='review'
  &&!q('.conversation-preview')&&!window.getSelection?.()?.toString();
}
function swipeSurface(target){
 if(target.closest?.('button,a,input,textarea,select,summary,[role="button"],.reader-receipt'))return null;
 const surface=target.closest?.('.conversation-list,#terminal-output');
 if(!surface)return null;
 for(let node=target;node;node=node.parentElement){
  const overflow=window.getComputedStyle(node).overflowX;
  if(node.scrollWidth>node.clientWidth+1&&['auto','scroll'].includes(overflow))return null;
  if(node===surface)break;
 }
 return surface;
}
sheet.addEventListener('touchstart',event=>{
 readerSwipe=null;
 const touch=event.touches.length===1?event.touches[0]:null;
 if(!touch||!swipeAvailable()||touch.clientX<=24||touch.clientX>=innerWidth-24)return;
 const surface=swipeSurface(event.target);if(!surface)return;
 readerSwipe={id:touch.identifier,x:touch.clientX,y:touch.clientY,dx:0,dy:0,surface,generation,ready:false};
},{passive:true});
sheet.addEventListener('touchmove',event=>{
 const gesture=readerSwipe,touch=event.touches.length===1?event.touches[0]:null;
 if(!gesture)return;
 if(!touch||touch.identifier!==gesture.id||!swipeAvailable()||gesture.generation!==generation){readerSwipe=null;return;}
 gesture.dx=touch.clientX-gesture.x;gesture.dy=touch.clientY-gesture.y;
 const across=Math.abs(gesture.dx),down=Math.abs(gesture.dy);
 if(down>12&&down>=across){readerSwipe=null;return;}
 gesture.ready=across>=64&&across>=down*2;
 if(gesture.ready)event.preventDefault();
},{passive:false});
sheet.addEventListener('touchend',event=>{
 const gesture=readerSwipe;readerSwipe=null;
 if(!gesture?.ready||!swipeAvailable()||gesture.generation!==generation||!gesture.surface.isConnected)return;
 const touch=[...event.changedTouches].find(value=>value.identifier===gesture.id);
 if(!touch)return;
 const dx=touch.clientX-gesture.x,dy=touch.clientY-gesture.y;
 if(Math.abs(dx)<64||Math.abs(dx)<Math.abs(dy)*2)return;
 event.preventDefault();switchTab(dx<0?'terminal':'conversation');
},{passive:false});
sheet.addEventListener('touchcancel',()=>{readerSwipe=null;},{passive:true});
function terminalText(pre,value){
 if(!pre._wrapLines){pre.textContent=value;return;}
 const lines=value.split('\n');
 pre.replaceChildren(...lines.map((line,index)=>{
  const node=make('span','terminal-output-line');
  if(/^[\s─━═┄┅┈┉\-_=+|┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬]{8,}$/.test(line))node.className+=' terminal-output-rule';
  else{
   let indent=0;for(const char of line.match(/^[ \t]*/)[0])indent+=char==='\t'?8-indent%8:1;
   node.style.setProperty('--terminal-indent',indent+'ch');
  }
  node.textContent=line+(index<lines.length-1?'\n':'');return node;
 }));
}
function choose(value){
 if(busy()||phase==='unknown')return;
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
  if(!checkingView&&(atBottom||!previewOK)){
   if(pre.textContent!==output)terminalText(pre,output);
   pre._preview=preview;
   pre.scrollTop=atBottom?pre.scrollHeight:top;
   if(!previewOK)pre._scrollView=null;
  }
 }
 const shown=pre?pre._preview:null,reading=pre&&pre._scrollView&&!pre._scrollView.following;
 const pending=previewOK&&shown&&shown.text!==output;
 const stream=q('.terminal-stream');if(stream)stream.textContent=previewOK?(pending?'New output · paused':reading?'Reading earlier output':'Updating every 3s'):'Preview unavailable';
 const age=q('.terminal-age');if(age)age.textContent=shown?'Shown '+new Date(shown.observed_at*1000).toLocaleTimeString('en-GB'):'';
 const limits=shown||preview;
 const note=q('.terminal-preview-note');if(note)note.textContent=[shown?.truncated?'Tail shortened to fit the snapshot limits.':'',shown?.controls_removed?'Control sequences removed.':'',pending?'Scroll to bottom to show the latest tail. Output between snapshots ('+(limits?.max_lines||120)+' lines / '+((limits?.max_bytes||12288)/1024)+' KiB) may be missing.':''].filter(Boolean).join(' ');
 const terminalState=q('.reader-terminal-state');if(terminalState)terminalState.textContent=previewOK?(pending?'New output · paused':reading?'Reading earlier output':''):'';
 const terminalMeta=q('.reader-terminal-meta');if(terminalMeta)terminalMeta.textContent=limits?'Latest '+(limits.max_lines||120)+' lines · up to '+((limits.max_bytes||12288)/1024)+' KiB · every 3s'+(shown?' · '+age.textContent:''):'Waiting for a verified terminal snapshot.';
 const dot=q('.reader-state');if(tab==='terminal'&&dot){dot.dataset.state=previewOK?'work':'unknown';dot.title=previewOK?'Terminal updating every 3s':'Terminal preview unavailable';dot.setAttribute('aria-label',dot.title);}
 const review=q('#terminal-review');if(review)review.disabled=phase!=='editing'||!previewOK||!!attachmentController||!inputValid();
 refreshReference();
 refreshAttachmentControls();
 const status=q('.terminal-message');if(status)status.textContent=message;
 // Metadata below the terminal can reflow after following was sampled above.
 // Account for that layout without treating a later scroll as a resize clamp.
 if(pre?._scrollView){
  const view=pre._scrollView,width=pre.clientWidth,height=pre.clientHeight;
  if(view.width!==width||view.height!==height){
   if(view.following)pre.scrollTop=pre.scrollHeight;
   // A shorter footer can clamp paused reading onto the new bottom.
   const clamped=view.clamped||!view.following&&pre.scrollHeight-height-pre.scrollTop<=2;
   pre._scrollView={...view,width,height,clamped};
  }
 }
}
async function poll(){
 if(!sheet.open||document.hidden||!choice||previewController)return;
 const g=generation,c=choice,controller=new AbortController();previewController=controller;
 try{
  const data=await request('preview',{epoch:capability.epoch,choice:c.choice},controller);
  if(g!==generation||previewController!==controller||document.hidden||!sheet.open||choice!==c)return;
  if(data.target?.choice!==c.choice||data.target?.name!==name||!sameTarget(data.target)||typeof data.target?.revision!=='string'||typeof data.text!=='string'||new TextEncoder().encode(data.text).length>12288)throw Error('unavailable');
  if(target&&data.target.revision!==target.revision){invalidate('Pane changed. Refresh panes and review again.');return;}
  target=Object.freeze({...data.target});preview=data;output=data.text;previewOK=true;checkingView=false;failures=0;
  if(!['preparing','review','sending','accepted','unknown'].includes(phase))message='';
  refreshOutput();
  probeAttachments().then(flushAttachmentSelection);
 }catch(_){
  if(g!==generation||previewController!==controller||!sheet.open||document.hidden)return;
  failures++;previewOK=false;output='';preview=null;
  if(phase==='review'){cancelIntent();phase='editing';message='Preview unavailable. Review again after a fresh update.';render();}
  else{message='Preview unavailable. Refresh panes if the target changed.';refreshOutput();}
 }finally{
  if(g===generation&&previewController===controller){previewController=null;if(sheet.open&&!document.hidden&&choice)pollTimer=setTimeout(poll,failures?Math.min(24000,3000*2**failures):3000);}
 }
}
function invalidate(reason){
 if(receipt&&['sending','unknown'].includes(phase)){previewOK=false;message=reason+' Receipt retained; check its status before writing again.';refreshOutput();return;}
 stop();cancelIntent();text='';output='';preview=null;previewOK=false;target=null;phase='changed';message=reason;render();
}
function inputError(value){
 if(/[\r\n]/u.test(value))return 'Line breaks cannot be sent. Use one line.';
 if(new TextEncoder().encode(value).length>4096)return 'Input exceeds 4096 bytes. Shorten the text or remove an image.';
 if(/[\p{Cc}\p{Cs}\u2028\u2029\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/u.test(value))return 'Unsupported characters. Remove control characters before reviewing.';
 return '';
}
function validText(value){return typeof value==='string'&&value.trim()&&!inputError(value);}
function clearAttachments(){for(const attachment of attachments)if(attachment.url)URL.revokeObjectURL(attachment.url);attachments=[];}
const draftText=()=>[text.replace(/[\r\n]+$/u,''),...attachments.map(attachment=>attachment.marker)].filter(Boolean).join(' ');
const inputText=()=>{
 if(!replyReference)return draftText();
 const r=replyReference.value;
 return 'Reference: '+r.agent+' · '+r.role+' · '+r.ts+' | Recorded message: '+r.eventId+' | Quote: '+JSON.stringify(r.text)+' | Reply: '+draftText();
};
const inputValid=()=>validText(draftText())&&validText(inputText())&&referenceCurrent(replyReference);
function inputErrorNote(){
 const note=make('p','terminal-input-error');note.id='terminal-input-error';note.setAttribute('role','status');
 note.refresh=()=>{note.textContent=referenceError()||inputError(inputText());note.hidden=!note.textContent;};note.refresh();return note;
}
function refreshAttachmentControls(){
 const add=q('#terminal-image-add'),panel=add?.closest('.reader-input-bar');
 if(!panel)return;
 panel.refreshAttachmentPicker?.();
 layoutInputBar(panel);
}
async function probeAttachments(){
 if(!target||attachmentProbe===target.choice+target.revision||attachmentProbeController)return;
 const g=generation,t=target,controller=new AbortController();attachmentProbeController=controller;
 attachmentProbe=t.choice+t.revision;
 await Promise.all(['image','file'].map(async kind=>{
  try{
   const result=await request(kind,{epoch:capability.epoch,choice:t.choice,revision:t.revision,[kind]:null},controller,10000);
   if(g!==generation||controller.signal.aborted||!sheet.open||target?.choice!==t.choice||
    result.target?.choice!==t.choice||result.target?.revision!==t.revision||!sameTarget(result.target))return;
   attachmentAccess[kind]=result.enabled===true;refreshAttachmentControls();
  }catch(_){
   if(g!==generation||controller.signal.aborted)return;
   attachmentAccess[kind]=false;refreshAttachmentControls();
   if(kind==='image')attachmentProbe='';
  }
 }));
 if(attachmentProbeController===controller)attachmentProbeController=null;
}
function renderAttachments(panel,removable){
 if(!attachments.length)return;
 const list=make('div','reader-images');list.setAttribute('aria-label','Attachments');
 const imageTotal=attachments.filter(item=>item.kind==='image').length;
 let imageIndex=0;
 for(const attachment of attachments){
  const box=make('div','reader-image');
  const caption=attachment.kind==='image'?'Image '+(++imageIndex)+' of '+imageTotal:attachment.label;
  if(attachment.url){
   const image=make('img','reader-image-thumb');image.src=attachment.url;image.alt='Attached image '+imageIndex;box.append(image);
  }else{
   const icon=make('span','reader-file-mark');icon.innerHTML='<svg class="mobile-icon" aria-hidden="true"><use href="#icon-file"/></svg>';box.append(icon);
  }
  const label=make('span','reader-attachment-label',caption);label.title=caption;box.append(label);
  if(removable){
   const remove=action('×','reader-image-remove',()=>{
    attachments=attachments.filter(value=>value!==attachment);if(attachment.url)URL.revokeObjectURL(attachment.url);render();
   });
   remove.setAttribute('aria-label',attachment.kind==='image'?'Remove attached image '+imageIndex:'Remove '+caption);
   remove.disabled=!!attachmentController;box.append(remove);
  }
  list.append(box);
 }
 panel.append(list);
}
function attachmentType(file,source){
 const extension=/\.([a-z]+)$/i.exec(file.name)?.[1].toLowerCase();
 if(attachmentAccess.image&&(imageTypes.includes(file.type)||!file.type&&imageExtensions.includes(extension)))return {kind:'image'};
 if(source==='file'&&attachmentAccess.file&&fileFormats.includes(extension))return {kind:'file',format:extension};
 return null;
}
function flushAttachmentSelection(){
 if(!pendingAttachmentSelection||!previewOK||document.hidden||attachmentProbeController)return;
 const {files,source,revision,epoch}=pendingAttachmentSelection;
 pendingAttachmentSelection=null;
 if(target?.revision===revision&&capability?.epoch===epoch)uploadAttachments(files,source);
}
function readAttachment(file,signal){
 return new Promise((resolve,reject)=>{
  const reader=new FileReader(),cleanup=()=>signal.removeEventListener('abort',abort);
  const abort=()=>{reader.abort();cleanup();reject(Error('cancelled'));};
  reader.onload=()=>{cleanup();const url=reader.result;resolve(url.slice(url.indexOf(',')+1));};
  reader.onerror=()=>{cleanup();reject(Error('unavailable'));};
  signal.addEventListener('abort',abort,{once:true});reader.readAsDataURL(file);
 });
}
async function uploadAttachments(files,source){
 const selected=Array.from(files||[]);
 if(!selected.length||!target||!previewOK||attachmentController||!['editing','accepted','refused'].includes(phase))return;
 const typed=selected.map(file=>({file,type:attachmentType(file,source)}));
 const includesFiles=typed.some(({type})=>type?.kind==='file')||attachments.some(item=>item.kind==='file');
 if(attachments.length+selected.length>MAX_ATTACHMENTS){
  message='Attach up to '+MAX_ATTACHMENTS+(includesFiles?' items':' images')+' per input.';render();return;
 }
 for(const {file,type} of typed){
  if(!type){message=source==='file'&&attachmentAccess.file?'Choose a JPEG, PNG, WebP, PDF, TXT, MD, CSV or JSON file.':'Choose a JPEG, PNG or WebP image.';render();return;}
  const limit=type.kind==='image'?10:5;
  if(file.size>limit*1024*1024){message=(type.kind==='image'?'Choose an image up to ':'Choose a document up to ')+limit+' MiB.';render();return;}
  if(type.kind==='file'&&!file.size){message='Choose a document that is not empty.';render();return;}
 }
 if(phase!=='editing'){cancelIntent();phase='editing';}
 const g=generation,t=target,controller=new AbortController();attachmentController=controller;
 let kind=source==='photo'?'image':'file';
 try{
  for(const [index,{file,type}] of typed.entries()){
   if(g!==generation||controller.signal.aborted)return;
   kind=type.kind;
   message='Uploading '+(kind==='image'?'image ':'file ')+(index+1)+' of '+typed.length+'…';render();
   const data=await readAttachment(file,controller.signal);
   if(g!==generation||controller.signal.aborted)return;
   const result=await request(kind,{epoch:capability.epoch,choice:t.choice,revision:t.revision,
    [kind]:kind==='image'?data:{format:type.format,data}},controller,30000);
   if(g!==generation||!sheet.open||document.hidden)return;
   if(result.enabled!==true||result.target?.choice!==t.choice||result.target?.revision!==t.revision||
    !sameTarget(result.target)||typeof result.path!=='string'||!result.path.startsWith('/'))throw Error('unavailable');
   const quoted="'"+result.path.replaceAll("'","'\\''")+"'",marker='['+kind+': '+quoted+']';
   if(!validText([inputText(),marker].filter(Boolean).join(' ')))throw Error(kind+'_text_too_large');
   attachments.push({kind,marker,label:file.name,url:kind==='image'?URL.createObjectURL(file):null});
  }
  message='';
 }catch(error){
  if(g===generation){
   const reasons={
    image_folder_full:'This workspace already has 50 images. Remove older files before attaching another.',
    image_too_large:'Choose an image up to 10 MiB.',image_pixels_too_large:'Choose an image up to 24 megapixels.',
    image_format_unsupported:'Choose a static JPEG, PNG or WebP image.',
    image_text_too_large:'The image paths and text exceed 4096 bytes. Shorten the text or remove an image.',
    file_folder_full:'This workspace already has 50 documents. Remove older files before attaching another.',
    file_too_large:'Choose a document up to 5 MiB.',file_empty:'Choose a document that is not empty.',
    file_format_unsupported:'Choose a PDF or UTF-8 TXT, MD, CSV or JSON file.',
    file_text_too_large:'The attachment paths and text exceed 4096 bytes. Shorten the text or remove an attachment.',
   };
   const noun=includesFiles?' attachment(s)':' image(s)';
   const failed=kind==='image'?'Image':'Document';
   message=(attachments.length?attachments.length+noun+' attached. ':'')+(reasons[error.message]||failed+' could not be saved. No input was sent. Choose the file again.');
  }
 }finally{if(g===generation&&attachmentController===controller){attachmentController=null;render();}}
}
async function reviewInput(){
 if(phase!=='editing'||attachmentController||!target||!choice||!previewOK||!inputValid())return;
 composer=false;
 clearTimeout(pollTimer);previewController?.abort();previewController=null;
 const g=generation,t=target,draft=inputText(),reference=replyReference,controller=new AbortController();operationController=controller;phase='preparing';render();
 try{
  const data=await request('prepare',{epoch:capability.epoch,choice:t.choice,revision:t.revision,text:draft},controller);
  if(g!==generation||!sheet.open||document.hidden)return;
  if(data.state!=='prepared'||data.text!==draft||data.expires_in!==30||data.target?.revision!==t.revision||data.target?.choice!==t.choice||data.target?.name!==name||!sameTarget(data.target))throw Error('unavailable');
  if(!referenceCurrent(reference))throw Error('reference_changed');
  preparedReference=reference;prepared=Object.freeze({...data,target:Object.freeze({...data.target})});reviewUntil=Date.now()+data.expires_in*1000;receipt=null;phase='review';message='';render();
  const identity=prepared;expiryTimer=setTimeout(()=>{if(prepared!==identity||document.hidden)return;cancelIntent();phase='editing';message='Confirmation expired. Review again.';render();},data.expires_in*1000);
 }catch(_){if(g===generation&&sheet.open){prepared=null;phase='editing';message='Could not prepare this input. Refresh the pane and review again.';render();}}
 finally{if(g===generation){operationController=null;if(!document.hidden&&sheet.open)pollTimer=setTimeout(poll,3000);}}
}
async function send(){
 if(phase!=='review'||!prepared||!previewOK)return;
 if(!referenceCurrent(preparedReference)){cancelIntent();phase='editing';message='';render();return;}
 sendFeedback?.begin(q('.reader-send-confirm'));
 const sentText=prepared.text,p={epoch:prepared.epoch,intent:prepared.intent,confirmation:prepared.confirmation,target:prepared.target};
 prepared=null;preparedReference=replyReference=null;receipt=p;text='';clearAttachments();clearTimeout(expiryTimer);clearTimeout(pollTimer);previewController?.abort();previewController=null;
 const g=generation,controller=new AbortController();operationController=controller;phase='sending';message='';render();
 try{
  const result=await request('commit',{epoch:p.epoch,intent:p.intent,confirmation:p.confirmation},controller,7000);
  if(g!==generation||!sheet.open||document.hidden)return;
  if(result.intent!==p.intent||result.target?.revision!==p.target.revision||!sameTarget(result.target))throw Error('unavailable');
  phase=result.state==='accepted'?'accepted':result.state==='refused'||result.state==='cancelled'||result.state==='expired'?'refused':'unknown';
  // Local display only, until the same words appear in the verified record.
  if(phase==='accepted'&&history){tab='conversation';composer=false;conversation()?.addPending(sentText);}
  if(phase==='accepted')scheduleReceiptDismissal();
  render();
 }catch(_){if(g===generation&&sheet.open){phase='unknown';render();}}
 finally{if(g===generation){operationController=null;if(!document.hidden&&sheet.open)poll();}}
}
function scheduleReceiptDismissal(){
 clearTimeout(receiptTimer);
 const shown=receipt;
 receiptTimer=setTimeout(()=>{
  if(!sheet.open||phase!=='accepted'||receipt!==shown)return;
  receipt=null;phase='editing';sendFeedback?.reset();render();
 },3000);
}
async function status(){
 if(!receipt||operationController)return;const g=generation,p=receipt,controller=new AbortController();operationController=controller;
 try{const result=await request('status',{epoch:p.epoch,intent:p.intent},controller);
  if(g!==generation||!sheet.open)return;
  if(result.intent!==p.intent||result.target?.revision!==p.target.revision||!sameTarget(result.target))throw Error('unavailable');
  phase=result.state==='accepted'?'accepted':result.state==='refused'||result.state==='cancelled'||result.state==='expired'?'refused':'unknown';
  if(phase==='accepted')scheduleReceiptDismissal();
  render();
 }catch(_){if(g===generation&&sheet.open){message='Receipt unavailable. Input was not sent again.';render();}}
 finally{if(g===generation)operationController=null;}
}
function resizeSheet(){
 if(!sheet.open)return;const v=window.visualViewport,h=v?.height||innerHeight,offset=v?.offsetTop||0;
 sheet.style.height=(history?h:Math.min(746,h-(h<610?8:86)))+'px';sheet.style.maxHeight=(history?h:Math.max(120,h-8))+'px';
 sheet.style.bottom=Math.max(0,innerHeight-h-offset)+'px';sheet.classList.toggle('terminal-keyboard',h<610);
 refreshOutput();
}
window.visualViewport?.addEventListener('resize',resizeSheet);window.visualViewport?.addEventListener('scroll',resizeSheet);window.addEventListener('resize',resizeSheet);
function dismissLayer(){
 if(conversation()?.dismissPreview?.())return true;
 if(composer){composer=false;sheet.focus({preventScroll:true});return true;}
 return false;
}
function changePane(){if(busy()||phase==='unknown')return;window.OrreryMobileComposer?.fold();composer=false;tab='terminal';return list();}
function paneButton(label){const b=action(label,'terminal-pane-button',changePane);b.disabled=busy()||phase==='unknown';return b;}
function readerBar(){
 const bar=make('div','reader-bar'),who=make('div','reader-person'),portrait=make('img');portrait.src=window.AGENTSTACK_DEMO.portraitURL(name);portrait.alt='';portrait.onerror=()=>{portrait.remove();};
 const initials=make('span','reader-initials',name.slice(0,2)),title=make('h2','',name);title.id='terminal-title';who.append(initials,portrait,title,make('span','reader-state'));bar.append(who);
 const icons={conversation:['Conversation','M3 4h6l3 2 3-2h6v15h-6l-3 2-3-2H3z M12 6v15'],terminal:['Terminal','m5 7 5 5-5 5 M13 17h6'],close:['Close Conversation','m6 6 12 12 M18 6 6 18']};
 for(const value of ['conversation','terminal','close']){
  if(value==='terminal'&&!enabled)continue;
  const [label,path]=icons[value],b=action('','reader-icon '+(value==='close'?'terminal-close':''),()=>value==='close'?close():switchTab(value));b.setAttribute('aria-label',label);b.title=label;
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'),line=document.createElementNS('http://www.w3.org/2000/svg','path');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');line.setAttribute('d',path);svg.append(line);b.append(svg);
  if(['conversation','terminal'].includes(value)){b.setAttribute('aria-pressed',String(tab===value));b.disabled=busy();}
  bar.append(b);
 }
 const bell=window.OrreryMobilePush?.button(name);if(bell){bell.disabled=busy();bar.insertBefore(bell,bar.lastElementChild);}
 if(!pane){const add=window.OrreryMobileWorkspace?.readerEntry(name,selector);if(add)bar.insertBefore(add,bar.lastElementChild);}
 sheet.append(bar);queueMicrotask(()=>{conversation()?.refreshActivity?.();refreshOutput();});
}
function fitInput(field,animate=false){
 if(readerMotion){readerMotion.fit(field,animate);return;}
 field.style.height='44px';field.style.height=Math.min(120,Math.max(44,field.scrollHeight))+'px';
}
function layoutInputBar(panel){
 if(!panel.isConnected)return;
 const field=panel.querySelector('#terminal-text');
 if(field&&field._inputWidth!==field.clientWidth){field._inputWidth=field.clientWidth;fitInput(field);}
 const space=Math.ceil(panel.getBoundingClientRect().height)+12+'px';
 if(sheet.style.getPropertyValue('--reader-input-space')!==space){sheet.style.setProperty('--reader-input-space',space);refreshOutput();}
}
function attachmentPicker(panel,row,available){
 const sources=make('div','reader-attachment-sources');sources.id='reader-attachment-sources';sources.hidden=true;
 sources.setAttribute('role','group');sources.setAttribute('aria-label','Choose an attachment source');
 const add=action('','reader-image-add',()=>show(sources.hidden,true));add.id='terminal-image-add';
 add.title='Attach';add.setAttribute('aria-label','Attach');add.setAttribute('aria-controls',sources.id);
 add.innerHTML='<svg class="mobile-icon" aria-hidden="true"><use href="#icon-attachment"/></svg>';
 function show(open,focus=false){
  sources.hidden=!open;add.setAttribute('aria-expanded',String(open));
  if(focus)(open?sources.querySelector('label:not([hidden]) input:enabled'):add)?.focus({preventScroll:true});
  layoutInputBar(panel);
 }
 const choices={};
 for(const [kind,id] of [['photo','terminal-image-file'],['file','terminal-file-image']]){
  const label=make('label','icon-button');
  label.htmlFor=id;
  label.innerHTML='<svg class="mobile-icon" aria-hidden="true"><use href="#icon-'+kind+'"/></svg>';
  const input=make('input');input.type='file';input.id=id;input.multiple=true;
  input.onchange=()=>{
   const files=Array.from(input.files);
   input.value='';
   if(!input.isConnected)return;
   show(false,true);
   if(!files.length||!target)return;
   // A native picker can deliver its selection while the pane is being reverified.
   if(document.hidden||checkingView||attachmentProbeController){
    pendingAttachmentSelection={files,source:kind,revision:target.revision,epoch:capability.epoch};
   }else uploadAttachments(files,kind);
  };
  input.oncancel=()=>show(false,true);
  choices[kind]={input,label};label.append(input);sources.append(label);
 }
 panel.refreshAttachmentPicker=()=>{
  const {image,file}=attachmentAccess;
  add.hidden=!image&&!file;
  add.disabled=!available||!previewOK||!!attachmentController||attachments.length>=MAX_ATTACHMENTS;
  if(add.hidden||add.disabled){sources.hidden=true;add.setAttribute('aria-expanded','false');}
  choices.photo.label.hidden=!image;choices.photo.input.accept=imageTypes.join(',');
  if(file)choices.file.input.removeAttribute('accept');
  else choices.file.input.accept=imageExtensions.map(format=>'.'+format).join(',');
  for(const [choice,description] of [[choices.photo,'Choose a photo'],[choices.file,file?'Choose a file':'Choose an image from files']]){
   choice.input.disabled=add.disabled||choice.label.hidden;
   choice.label.title=description;choice.input.setAttribute('aria-label',description);
  }
 };
 panel.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&!sources.hidden){event.preventDefault();show(false,true);}
 });
 panel.addEventListener('focusout',event=>{
  if(!sources.hidden&&!sources.contains(event.target)&&event.relatedTarget&&!panel.contains(event.relatedTarget))show(false);
 });
 add.setAttribute('aria-expanded','false');panel.refreshAttachmentPicker();panel.append(sources);row.append(add);
}
function readerInputBar(){
 const panel=make('div','reader-composer reader-input-bar'+(phase==='review'?' reader-review':''));panel.setAttribute('role','region');panel.setAttribute('aria-label',phase==='review'?'Review input':'Write input');
 let receiptBox=null;
 panel.classList.toggle('has-reference',!!replyReference);
 const shown=phase==='review'?prepared.target:choice;
 const destination=paneButton((shown?shown.name+' · '+shown.pane_label:'Choose pane')+' ⌄');destination.className+=' reader-input-target';destination.setAttribute('aria-label','Change pane for '+name+(shown?' · '+shown.pane_label:''));
 panel.append(destination);
 if(replyReference&&phase!=='review'){
  const r=replyReference.value,card=make('section','reader-reference-card');card.setAttribute('aria-label','Reply reference');
  const at=new Date(r.ts),clock=Number.isFinite(at.getTime())?at.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}):'';
  const label=make('div','reader-reference-label',r.agent+' · Selected text'+(clock?' · '+clock:''));
  const quote=make('blockquote','reader-reference-quote',r.text);quote.title=r.text;
  const remove=action('×','reader-reference-remove',()=>{replyReference=null;render();q('#terminal-text')?.focus({preventScroll:true});});remove.setAttribute('aria-label','Remove reference');remove.disabled=busy();
  card.append(label,quote,remove);panel.append(card);
 }
 if(phase==='review'){
  panel.append(make('h3','',prepared.target.name+' — send this input?'),make('p','reader-not-sent','Not sent yet'),make('pre','terminal-exact',prepared.text));
  renderAttachments(panel,false);
  const enter=make('div','terminal-enter');enter.append(make('span','','After this text'),make('kbd','','Enter ↵'));
  panel.append(enter,make('p','terminal-help','Sends once to this pane. It may be a shell. Existing terminal input may be combined with it.'));
  const actions=make('div','terminal-actions'),confirm=action('Send once','terminal-primary',send);
  if(sendFeedback)confirm.classList.add('reader-send-confirm');
  actions.append(action('Edit','',()=>{cancelIntent();phase='editing';composer=true;render();queueMicrotask(()=>q('#terminal-text')?.focus({preventScroll:true}));}),confirm);panel.append(actions);
 }else{
  if(['sending','accepted','unknown','refused'].includes(phase))receiptBox=readerReceipt(panel);
  const available=!!choice&&['editing','accepted','refused'].includes(phase),label=make('label','reader-input-label','Your input');label.htmlFor='terminal-text';
  const row=make('div','reader-input-row'),field=make('textarea','terminal-text');field.id='terminal-text';field.rows=1;field.value=text;field.placeholder=choice?'Write one line…':'Choose a pane first';field.autocomplete='off';field.spellcheck=false;field.disabled=!available;
  const review=action(phase==='preparing'?'…':'↑','terminal-primary reader-input-send',reviewInput);review.id='terminal-review';review.title='Review input';review.setAttribute('aria-label','Review input');review.disabled=!available||!previewOK||!!attachmentController||!inputValid();
  const count=make('span','reader-input-count',new TextEncoder().encode(inputText()).length+' / 4096 bytes');count.hidden=!inputText();
  const error=inputErrorNote();field.setAttribute('aria-describedby','terminal-input-error');
  field.onfocus=()=>{composer=true;};field.onblur=()=>{composer=false;};
  field.oninput=()=>{
   if(!['editing','accepted','refused'].includes(phase))return;
   if(['accepted','refused'].includes(phase)){cancelIntent();sendFeedback?.reset();phase='editing';panel.querySelector('.reader-receipt')?.remove();}
   text=field.value;count.textContent=new TextEncoder().encode(inputText()).length+' / 4096 bytes';count.hidden=!inputText();review.disabled=!previewOK||!!attachmentController||!inputValid();refreshReference();fitInput(field,!!inputObserver);layoutInputBar(panel);
  };
  field.onkeydown=e=>{if(e.key==='Enter'&&!e.isComposing)e.preventDefault();};
  if(!choice)panel.append(make('p','terminal-help','Choose a verified pane in Terminal to write.'));
  renderAttachments(panel,true);attachmentPicker(panel,row,available);row.append(field,review);panel.append(label,row,count,error);
 }
 sheet.append(panel);
 if(receiptBox)sendFeedback?.show(receiptBox,phase);
 layoutInputBar(panel);
 if(typeof ResizeObserver!=='undefined'){inputObserver=new ResizeObserver(()=>layoutInputBar(panel));inputObserver.observe(panel);}
}
function readerReceipt(host){
 const accepted=phase==='accepted',unknown=phase==='unknown',sending=phase==='sending',box=make('div','reader-receipt '+phase);box.setAttribute('role','status');
 box.append(make('strong','',accepted?'Received by terminal':unknown?'Outcome unknown':sending?'Sending once…':'Input not sent'));
 box.append(make('small','',accepted?'Input and Enter received. Completion is not confirmed.':unknown?'Some input may have arrived. No automatic retry.':sending?'Waiting for the terminal receipt.':'Input refused or confirmation expired.'));
 if(unknown)box.append(action('Check receipt','',status));
 host.append(box);return box;
}
function render(){
 conversation()?.dismissReferenceSelection?.();
 rememberView();
 inputObserver?.disconnect();inputObserver=null;
 const focus=document.activeElement,hadFocus=focus&&sheet.contains?.(focus);if(hadFocus){sheet.tabIndex=-1;sheet.focus({preventScroll:true});}
 const previous=q('#terminal-output');
 // Review/edit rebuild the sheet for the same pane. Choosing a pane and
 // reopening clear output first, so their new preview still opens at its tail.
 const readTop=(previewOK||checkingView)&&previous&&!terminalFollowing(previous)?previous.scrollTop:null;
 const readOutput=readTop!==null?previous.textContent:'',readPreview=readTop!==null?previous._preview:null,readView=readTop!==null?previous._scrollView:null;
 resizeSheet();sheet.replaceChildren();if(hadFocus)queueMicrotask(()=>{if(focus.isConnected)focus.focus({preventScroll:true});else if(focus.id)q('#'+focus.id)?.focus({preventScroll:true});});
 // Review keeps the conversation mounted and shows only the target and text.
 // Sending still requires explicit confirmation against the verified pane.
 const reading=tab==='conversation';
 sheet.classList.toggle('reader-fullscreen',history);
 if(history)readerBar();else{
 const header=make('div','terminal-header'),title=make('h2','',phase==='review'?'Review input':reading?'Conversation':'Terminal');title.id='terminal-title';header.append(title,action('Close','terminal-close',close));sheet.append(header);
 const who=make('div','terminal-identity'),copy=make('div'),portrait=make('img');portrait.src=window.AGENTSTACK_DEMO.portraitURL(name);portrait.alt='';portrait.onerror=()=>portrait.remove();copy.append(make('strong','',name),make('small','',reading?'Conversation record · read-only':'Cockpit session'));who.append(portrait,copy);
 if(choice)who.append(paneButton(choice.pane_label+' ⌄'));
 sheet.append(who);
 if(history&&enabled){
  const tabs=make('div','conversation-tabs');tabs.setAttribute('role','tablist');
  for(const [value,label]of[['conversation','Conversation'],['terminal','Terminal']]){const b=make('button','',label);b.type='button';b.setAttribute('role','tab');b.setAttribute('aria-selected',String(tab===value));b.disabled=busy();b.onclick=()=>switchTab(value);tabs.append(b);}
  sheet.append(tabs);
 }
 }
 let scroll=make('div','terminal-scroll'+(reading?' conversation-mode':history?' reader-terminal':''));sheet.append(scroll);
 if(reading){
  const view=conversation()?.element()||conversation()?.mount(name);if(view){scroll.append(view);queueMicrotask(()=>{conversation()?.restore();if(focus?.isConnected&&hadFocus)focus.focus({preventScroll:true});});}
  const warning=make('p','terminal-message',message);warning.setAttribute('role','status');sheet.append(warning);
  if(phase==='history-only')return;
 }
 if(phase==='loading'){if(!reading)scroll.append(make('p','terminal-help','Finding selectable panes…'));if(history)readerInputBar();return;}
 if(phase==='choose'){
  scroll.append(make('p','terminal-help','Choose the pane you want to view and send input to.'));
  const paneList=make('div','terminal-choices');for(const c of choices){const b=action('','',()=>choose(c));b.append(make('strong','',c.pane_label),make('small','',c.name));paneList.append(b);}scroll.append(paneList);
  if(message)scroll.append(make('p','terminal-help',message));scroll.append(action('Refresh panes','',list));if(history)readerInputBar();return;
 }
 if(['unavailable','changed'].includes(phase)){
  if(!reading)scroll.append(make('p','terminal-result',phase==='changed'?'Pane changed':'Terminal unavailable'),make('p','terminal-help',message));
  if(history){if(!reading)scroll.append(action('Refresh panes','',list));readerInputBar();}else sheet.append(action('Refresh panes','terminal-button',list));return;
 }
 let pre=null,actionsHost=sheet;
 if(!reading){
 const terminal=make('div','terminal-preview'),bar=make('div','terminal-preview-head');bar.append(make('span','terminal-stream',''),make('span','','Terminal tail'));pre=make('pre','terminal-output');pre.id='terminal-output';pre._wrapLines=history;terminal.append(bar,pre);scroll.append(terminal);
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
 const observed=make('div','terminal-observed');observed.append(make('span','','Latest '+(preview?.max_lines||120)+' lines · up to '+((preview?.max_bytes||12288)/1024)+' KiB'),make('span','terminal-age',''));scroll.append(observed,make('p','terminal-preview-note',''));
 if(history){scroll.append(make('p','reader-terminal-meta'));const state=make('p','reader-terminal-state');state.setAttribute('role','status');scroll.append(state);}
 }
 if(history){
  if(readTop!==null&&pre){terminalText(pre,readOutput);pre._preview=readPreview;pre._scrollView=readView;pre.scrollTop=readTop;}
  readerInputBar();if(!reading)scroll.append(make('p','terminal-message',message));refreshOutput();return;
 }
 if(['editing','preparing'].includes(phase)){
  const label=make('label','terminal-compose-label','Your input');label.htmlFor='terminal-text';const field=make('textarea','terminal-text');field.id='terminal-text';field.rows=2;field.value=text;field.placeholder='Write one line…';field.autocomplete='off';field.spellcheck=false;field.disabled=phase==='preparing';
  const count=make('span','',new TextEncoder().encode(inputText()).length+' / 4096 bytes'),meta=make('div','terminal-input-meta');meta.append(make('span','','One line + Enter'),count);
  const error=inputErrorNote();field.setAttribute('aria-describedby','terminal-input-error');
  scroll.append(label,field,meta,error,make('p','terminal-help',reading?'Sends to '+choice.pane_label+'. Review the destination and exact text before sending.':'Check the terminal above before sending.'));
  const actions=make('div','terminal-actions'),review=action(phase==='preparing'?'Preparing…':'Review input','terminal-primary',reviewInput);review.id='terminal-review';review.disabled=phase==='preparing'||!previewOK||!validText(inputText());
  field.oninput=()=>{text=field.value;count.textContent=new TextEncoder().encode(inputText()).length+' / 4096 bytes';review.disabled=!previewOK||!validText(inputText());error.refresh();};field.onkeydown=e=>{if(e.key==='Enter')e.preventDefault();};actions.append(action('Cancel','',close),review);actionsHost.append(actions);
 }else if(phase==='review'){
  scroll.append(make('p','terminal-compose-label','Exact text to '+prepared.target.name),make('pre','terminal-exact',prepared.text));const enter=make('div','terminal-enter');enter.append(make('span','','After this text'),make('kbd','','Enter ↵'));scroll.append(enter,make('p','terminal-help','Sends once to this pane. It may be a shell. Existing terminal input may be combined with it.'));
  const actions=make('div','terminal-actions');actions.append(action('Edit','',()=>{cancelIntent();phase='editing';render();}),action('Send once','terminal-primary',send));actionsHost.append(actions);
 }else{
  const accepted=phase==='accepted',unknown=phase==='unknown';scroll.append(make('p','terminal-result',accepted?'Received by terminal':unknown?'Outcome unknown':phase==='sending'?'Sending once…':'Input not sent'),make('p','terminal-receipt',accepted?'Input and Enter received. This does not confirm the task is complete.':unknown?'Some input may have arrived. No automatic retry. Check the pane before writing again.':phase==='sending'?'Waiting for the terminal receipt.':'The input was refused or its confirmation expired.'));
  if(accepted)scroll.append(make('p','terminal-result-note','The pane keeps updating while this sheet is open.'));
  const actions=make('div','terminal-actions');actions.append(action('Close','',close));if(unknown)actions.append(action('Check receipt','terminal-primary',status));else if(phase!=='sending')actions.append(action('Write again','terminal-primary',()=>{cancelIntent();phase='editing';text='';render();}));sheet.append(actions);
 }
 if(!reading)scroll.append(make('p','terminal-message',message));
 if(readTop!==null&&pre){terminalText(pre,readOutput);pre._preview=readPreview;pre._scrollView=readView;pre.scrollTop=readTop;}
 refreshOutput();
}
sheet.addEventListener('keydown',e=>{if(e.key==='Escape'&&!e.defaultPrevented){e.preventDefault();if(!dismissLayer())close();}});
sheet.addEventListener('cancel',e=>{if(e.target!==sheet)return;e.preventDefault();if(!dismissLayer())close();});sheet.addEventListener('close',()=>{if(name&&!sheet.open)close();});
function pauseView(){
 if(!sheet.open)return;paused=true;checkingView=true;rememberView();clearTimeout(pollTimer);pollTimer=0;
 previewController?.abort();previewController=null;resumeController?.abort();resumeController=null;
 attachmentProbeController?.abort();attachmentProbeController=null;attachmentProbe='';
 attachmentController?.abort();attachmentController=null;operationController?.abort();operationController=null;generation++;
 // Settle the visible phase even if an already resolved response reaches its
 // hidden-page guard without rejecting the aborted request.
 if(phase==='sending'){phase='unknown';render();}
 else if(phase==='preparing'){phase='editing';message='Input was not sent. Review again.';render();}
 previewOK=false;
 const review=q('#terminal-review');if(review)review.disabled=true;
}
async function resumeView(){
 if(!sheet.open||document.hidden||!enabled)return;
 if(phase==='review'&&prepared&&Date.now()>=reviewUntil){cancelIntent();phase='editing';message='Confirmation expired. Review again.';render();}
 if(!choice){if(!busy()&&phase!=='unknown')list();return;}
 const g=generation,old=choice,controller=new AbortController();resumeController=controller;
 try{
  // Choices expire while Android suspends a tab. Renew the catalog, then
  // compare the selected pane's full revision in the existing preview path.
  const data=await request('targets',{epoch:capability.epoch,name,...(fresh()?selector:{})},controller);
  if(g!==generation||resumeController!==controller||document.hidden||!sheet.open)return;
  const candidates=Array.isArray(data.targets)?data.targets.filter(t=>t.name===name&&t.pane_label===old.pane_label&&sameTarget(t)):[];
  if(candidates.length!==1){invalidate('Pane changed. Refresh panes and review again.');return;}
  choice=candidates[0];await poll();
 }catch(_){if(g===generation&&!document.hidden&&sheet.open){
  // A transport failure does not establish that the pane changed. Retain the
  // draft/receipt, keep input disabled, and renew the catalog before polling.
  message='Pane unavailable. Rechecking before input is enabled.';refreshOutput();
  clearTimeout(pollTimer);pollTimer=setTimeout(resumeView,Math.min(24000,3000*2**Math.min(++failures,3)));
 }}
 finally{if(resumeController===controller)resumeController=null;}
}
function startPane(){if(startupReady&&!sheet.open&&!document.hidden&&(enabled||history))open(pane.name,pane.identity,'conversation');}
document.addEventListener('visibilitychange',()=>{if(document.hidden)pauseView();else if(paused){paused=false;resumeView();}else if(pane)startPane();else restoreSavedView();});
window.addEventListener('pagehide',()=>{rememberView();close(true,true);},true);
window.OrreryMobileTerminal={open,close,mail,restore:restoreSavedView,available:()=>enabled||history,inputAvailable:()=>enabled,reference:addReference,refreshReference,canReference:()=>phase==='editing'&&tab==='conversation'&&previewOK&&!!target,
 canLeave:()=>!busy()&&phase!=='unknown'&&!attachmentController};entries();
const historyChecked=window.OrreryMobileConversation?window.OrreryMobileConversation.enabled():Promise.resolve(false);
historyChecked.then(value=>{if(value!==true)return;history=true;const mode=document.querySelector('body>header>span');if(mode)mode.textContent=enabled?'History · Terminal':'History enabled';entries();});
const controller=new AbortController();const terminalChecked=request('',null,controller).then(value=>{if(((value.api===31&&value.supported==='legacy-managed-pane')||(value.api===34&&value.supported==='fresh-managed-pane'))&&value.enabled===true&&value.ack==='tmux-command-accepted'){capability=Object.freeze({...value});enabled=true;const mode=document.querySelector('body>header>span');if(mode)mode.textContent=history?'History · Terminal':'Terminal enabled';entries();}}).catch(()=>{});
Promise.all([historyChecked,terminalChecked]).then(()=>{
 startupReady=true;document.dispatchEvent?.(new Event('orrery-mobile-input-ready'));
 if(pane){if(enabled||history)startPane();else pane.unavailable();}
 else restoreSavedView();
});
})();
