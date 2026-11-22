/* Capability-admitted global dashboard. Legacy HTML driver is never run here. */
(async function(){
'use strict';
const F=window.OrreryMailFlow,E=F.element,B=F.button,$=s=>document.querySelector(s);
let viewer=null,roster=[],runtime=new Map(),view='deck',dialog=null,focusReturn=null;
const session=window.OrreryOperatorSession.create({onLock:()=>clearLocked(),onRequired:error=>unlock(error),onAccept:()=>start(),onAccepted:()=>{closeDialog();if(session.memoryOnly)say('BROWSER_SESSION_STORAGE_UNAVAILABLE · Memory only; reload requires Unlock.');}});
const api=session.api,stored=session.stored;
function clearMail(){mail.clear();}
function clearLocked(){const input=dialog?.querySelector('[name=capability]');if(input)input.value='';clearMail();operations.clear();roster=[];runtime.clear();$('#wrap').replaceChildren();$('#gsvg').replaceChildren();selector.replaceChildren();viewer=null;$('#netinfo').textContent='Locked';$('#mail-health').title='Locked';$('#mail-health').removeAttribute('data-status');$('#mail-health .mh-label').textContent='MAIL · LOCKED';$('#global-instruction-settings')?.replaceChildren();say('');}
function lock(){session.lock();}
function showDialog(title){
 if(dialog)dialog.remove();focusReturn=document.activeElement;
 dialog=E('div','global-dialog');dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-label',title);
 const form=E('form','global-form');form.append(E('h2','',title));dialog.append(form);document.body.append(dialog);return form;
}
function closeDialog(){dialog?.remove();dialog=null;focusReturn?.focus();}
function errorAt(form,error){let row=form.querySelector('.global-error');if(!row){row=E('p','global-error');row.setAttribute('role','alert');form.append(row);}row.textContent=String(error.message||error).slice(0,256);}
function unlock(error){
 if(error&&dialog)errorAt(dialog.querySelector('form'),error);
 if(dialog?.querySelector('[name=capability]'))return;
 const form=showDialog('Unlock ORRERY');form.append(E('p','','Run agentstack-http-operator show with this dashboard’s AGENTSTACK_HOME. Paste its capability here. It stays in this page session, never in a URL.'));
 const label=E('label','','Operator capability'),input=E('input');input.type='password';input.name='capability';input.autocomplete='off';input.required=true;label.append(input);form.append(label,B('Unlock',()=>form.requestSubmit()));
 form.onsubmit=async e=>{e.preventDefault();try{await session.accept(input.value.trim());}catch(error){errorAt(form,error);}};if(session.memoryOnly)form.append(E('p','','BROWSER_SESSION_STORAGE_UNAVAILABLE · Memory only; enter the capability again after reload.'));const help=E('p');help.append('Details: ');for(const [label,file] of [['English','global-delivery-client.en.md'],['日本語','global-delivery-client.md']]){const a=E('a','',label);a.href='https://github.com/gyroid-eth/orrery-telemetry/blob/master/docs/'+file+'#operator-ui-access';a.target='_blank';a.rel='noopener';help.append(a,' ');}form.append(help);input.focus();
}
const mail=window.OrreryMailController.create({session,drawer:$('#edrawer'),list:$('#ed-list'),say:message=>say(message)});
const openEdge=(...args)=>{mail.setViewer(viewer);return mail.openEdge(...args);};
const conversation=(...args)=>{mail.setViewer(viewer);return args[2]!=null?mail.openLegacyLabel(args[2]):mail.openMessage(args[0]);};
const instruction=agent=>mail.instruction(agent);
const tools=E('div','global-tools');
const viewerLabel=E('label','','View as '),selector=E('select');selector.setAttribute('aria-label','Mail viewpoint');viewerLabel.append(selector);
tools.append(viewerLabel,B('Lock',()=>lock()));$('header .spacer').before(tools);
const status=E('div','global-status');status.setAttribute('role','status');$('#wrap').before(status);
function say(message){status.textContent=message;}
for(const id of ['history','usage-pill','need-chip','st-network','st-display','st-usage']){const e=document.getElementById(id);if(e)e.hidden=true;}
$('header .gauges').hidden=true;
$('#q').oninput=renderRoster;
$('#settings-btn').onclick=()=>{settingsNotice().catch(error=>say(error.message));$('#settings').classList.add('on');$('#settings').setAttribute('aria-hidden','false');$('#settings-btn').setAttribute('aria-expanded','true');};
$('#settings-close').onclick=()=>{$('#settings').classList.remove('on');$('#settings').setAttribute('aria-hidden','true');$('#settings-btn').setAttribute('aria-expanded','false');$('#settings-btn').focus();};
for(const b of document.querySelectorAll('[data-theme-pref]'))b.onclick=()=>window.AgentStackColorTheme.setPreference(b.dataset.themePref);
for(const [id,mode] of [['v-deck','deck'],['v-net','net']])document.getElementById(id).onclick=()=>setView(mode);
function setView(mode){view=mode;document.body.dataset.view=mode;$('#v-deck').classList.toggle('on',mode==='deck');$('#v-net').classList.toggle('on',mode==='net');if(mode==='net')refreshGraph().catch(error=>say(error.message));}
function closeDrawer(){$('#edrawer').classList.remove('on');$('#edrawer').setAttribute('aria-hidden','true');focusReturn?.focus();}
$('#edrawer .ed-close').onclick=closeDrawer;
document.addEventListener('keydown',e=>{if(e.key==='Tab'&&dialog){const nodes=[...dialog.querySelectorAll('input,select,button,a[href]')].filter(x=>!x.disabled);const first=nodes[0],last=nodes.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}if(e.key==='Escape'){if(dialog&&!dialog.querySelector('[name=capability]'))closeDialog();else if($('#settings').classList.contains('on'))$('#settings-close').click();else closeDrawer();}});
selector.onchange=()=>{viewer=selector.value?Number(selector.value):null;clearMail();const u=new URL(location.href);if(viewer)u.searchParams.set('view_as_agent_id',viewer);else u.searchParams.delete('view_as_agent_id');u.searchParams.delete('message_id');u.searchParams.delete('thread_id');history.replaceState(null,'',u);refreshGraph().catch(error=>say(error.message));};
async function loadRoster(){
 const generation=session.epoch;let before=null,all=[];
 do{const j=await api('/api/agents?limit=200'+(before?'&before_agent_id='+before:''));all.push(...j.data.items);before=j.data.next_before_agent_id;}while(before&&all.length<10000);
 let observations=[];
 try{for(let at=0;at<all.length;at+=200){const ids=all.slice(at,at+200).map(a=>a.agent_id);if(ids.length)observations.push(...(await api('/api/runtime-observations?agent_ids='+ids.join(','))).items);}}
 catch(error){
  // Authentication/Lock changes the shared epoch; never restore a locked UI.
  if(generation!==session.epoch)throw error;
  observations=[];
 }
 if(generation!==session.epoch)return;
 roster=all;runtime=window.OrreryRuntimeNode.adapt(roster,observations);selector.replaceChildren(E('option','','Choose a viewpoint'));selector.firstChild.value='';
 for(const row of roster){const o=E('option','',row.name+' · #'+row.agent_id);o.value=row.agent_id;selector.append(o);}
 if(viewer&&roster.some(a=>a.agent_id===viewer))selector.value=viewer;else viewer=null;
 mail.setViewer(viewer);renderRoster();if(before)say('Roster is limited to 10,000 agents. Narrow the server inventory before continuing.');
}
function renderRoster(){const root=$('#wrap');root.replaceChildren();const list=E('div','global-crew');root.append(list);const q=$('#q').value.toLowerCase();
 for(const a of roster.filter(a=>(a.name+' '+a.task_description).toLowerCase().includes(q))){
  const b=B('',()=>{if(!viewer){say('Choose a Mail viewpoint first.');selector.focus();return;}focusReturn=b;openEdge(viewer,a.agent_id).catch(error=>say(error.message));},'global-agent');b.append(E('h2','',a.name),E('p','',a.program+' · '+a.model),E('p','',a.task_description||'No task recorded'),E('p','',runtime.get(a.agent_id).annot.role));list.append(b);
 }
 if(!roster.length)list.append(E('p','','No registered agents.'));
}
const svgNS='http://www.w3.org/2000/svg';function svg(tag,attrs,text){const e=document.createElementNS(svgNS,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,String(v));if(text!=null)e.textContent=text;return e;}
async function refreshGraph(){
 const root=$('#gsvg');
 const mine=viewer,generation=session.epoch;
 const j=mine?await api('/api/graph?view_as_agent_id='+mine+'&limit=100'):{data:{items:[],next_cursor:null}};if(mine!==viewer||generation!==session.epoch)return;
 const counts=new Map();for(const m of j.data.items)for(const r of [...m.to,...m.cc,...m.bcc]){if(m.sender.agent_id===r.agent_id)continue;const pair=[m.sender.agent_id,r.agent_id].sort((a,b)=>a-b),key=pair.join(':');const row=counts.get(key)||{pair,count:0};row.count++;counts.set(key,row);}
 root.replaceChildren();const width=root.clientWidth||1200,height=root.clientHeight||700,cx=width/2,cy=height/2,radius=Math.max(100,Math.min(width,height)*.29),positions=new Map();
 roster.forEach((a,i)=>positions.set(a.agent_id,{x:cx+Math.cos(i*2*Math.PI/Math.max(1,roster.length))*radius,y:cy+Math.sin(i*2*Math.PI/Math.max(1,roster.length))*radius}));
 for(const {pair,count} of counts.values()){const [a,b]=pair.map(id=>positions.get(id));if(!a||!b)continue;const group=svg('g',{tabindex:0,role:'button','aria-label':'Open visible Mail edge'});const attrs={x1:a.x,y1:a.y,x2:b.x,y2:b.y};group.append(svg('line',{...attrs,class:'global-edge'}),svg('line',{...attrs,class:'global-edge-hit'}),svg('text',{x:(a.x+b.x)/2,y:(a.y+b.y)/2,class:'global-node-label'},count));const open=()=>{focusReturn=group;openEdge(...pair).catch(error=>say(error.message));};group.onclick=open;group.onkeydown=e=>{if(e.key==='Enter')open();};root.append(group);}
 for(const a of roster){const p=positions.get(a.agent_id),g=window.OrreryNetworkNode.render(a.name,runtime.get(a.agent_id),{avatar:()=>null});g.setAttribute('transform',`translate(${p.x} ${p.y})`);g.setAttribute('tabindex','0');g.setAttribute('role','button');g.setAttribute('aria-label',a.name+' · '+(runtime.get(a.agent_id).annot.role));const title=svg('title',{},runtime.get(a.agent_id).annot.role);g.append(title);const open=()=>{if(!viewer){say('Choose a Mail viewpoint to open Mail.');selector.focus();return;}focusReturn=g;openEdge(viewer,a.agent_id).catch(error=>say(error.message));};g.onclick=open;g.onkeydown=e=>{if(e.key==='Enter')open();};root.append(g);}
 $('#netinfo').textContent=roster.length+' nodes · '+(mine?counts.size+' visible links'+(j.data.next_cursor?' · recent page only':''):'choose a Mail viewpoint for edges');
}
const operations=window.OrreryOperationController.create({session,showDialog,closeDialog,errorAt,getDialog:()=>dialog,refresh:loadRoster});
$('#newbtn').onclick=operations.open;

async function settingsNotice(){
 let root=$('#global-instruction-settings');if(!root){root=E('section');root.id='global-instruction-settings';$('#settings').append(root);}
 if(!viewer){root.replaceChildren(E('p','','Choose a Mail viewpoint to check instructions.'));return;}
 const mine=viewer,row=await instruction(mine);if(mine!==viewer)return;root.replaceChildren(E('h3','','Instructions'),F.notice(row,{force:true}));
 const d=await api('/api/deliverables');if(mine!==viewer)return;
 root.append(E('h3','','Deliverables'),E('p','',d.configured?'Only explicitly configured deliverable roots are searched.':'No deliverable roots configured. Set AGENTSTACK_DELIVERABLE_ROOTS; a work folder never expands this scope.'));
 for(const item of d.items[roster.find(a=>a.agent_id===mine)?.name]||[])root.append(E('p','',item.title+' · '+item.rel));
}
async function health(){
 const h=(await api('/api/mail-watcher-health')).data;
 $('#mail-health').title='Server readiness: '+h.server_ready+'; signal delivery: '+h.signal.status+'; pending: '+h.signal.pending_count+'; oldest age (seconds): '+(h.signal.oldest_pending_age_seconds??'none');
 $('#mail-health').dataset.status=h.signal.status;
 $('#mail-health .mh-label').textContent='MAIL · '+h.signal.status;
}
async function start(){
 const version=await api('/api/version');if(version.runtime_mode!=='global'||version.capabilities.global_dashboard_ui!==1)throw Error('GLOBAL_UI_CAPABILITY_REQUIRED');
 const route=new URLSearchParams(location.search),value=route.get('view_as_agent_id');viewer=value&&/^[1-9]\d*$/.test(value)?Number(value):null;
 await loadRoster();
 await health();
 setView(route.get('view')==='net'?'net':'deck');
 const mid=route.get('message_id'),old=route.get('thread_id');
 if(mid&&(!/^[1-9]\d*$/.test(mid)||old&&old!==mid))throw Error('REPLY_INPUT_CONFLICT');
 if((mid||old)&&!viewer){say('Choose a Mail viewpoint, then reopen this saved link.');return;}
 if(mid||old){if(old)say('This is an old Mail link. Copy the current URL after opening it.');if(mid||/^[1-9]\d*$/.test(old)){const id=Number(mid||old);await conversation(id);const u=new URL(location.href);u.searchParams.delete('thread_id');u.searchParams.set('message_id',id);history.replaceState(null,'',u);}else await conversation(null,null,old);}
}
await session.restore();
setInterval(()=>{if(session.unlocked&&!dialog){loadRoster().then(()=>health()).then(()=>view==='net'?refreshGraph():null).catch(error=>say(error.message));}},15000);
setInterval(()=>{if(session.unlocked&&viewer&&mail.page){mail.refreshNotice().catch(error=>say(error.message));}},60000);
window.OrreryGlobalDashboard=Object.freeze({lock,refresh:()=>start()});
})();
