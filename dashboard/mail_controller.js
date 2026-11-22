/* One Mail flow controller for the dashboard drawer and cockpit right pane. */
(function(){'use strict';
function create({session,drawer,list,say=()=>{}}){
const F=window.OrreryMailFlow,E=F.element,B=F.button,api=session.api;
let viewer=null,seq=0,edge=null,page=null,selected=null,notice=null,conversationQuery=null;
function clear(){seq++;edge=null;page=null;selected=null;notice=null;conversationQuery=null;list.replaceChildren();drawer.classList.remove('on');drawer.setAttribute('aria-hidden','true');drawer.querySelector('.ed-a').textContent='—';drawer.querySelector('.ed-b').textContent='—';drawer.querySelector('.ed-cnt').textContent='0';}
async function bodies(messages,mine=viewer){
 let at=0;const out=new Array(messages.length);
 await Promise.all(Array.from({length:Math.min(4,messages.length)},async()=>{for(;;){const i=at++;if(i>=messages.length)return;const m=messages[i];out[i]=m.body_md==null?(await api('/api/mail/message?view_as_agent_id='+mine+'&id='+m.id)).data:m;}}));return out;
}
async function instruction(agent){try{return (await api('/api/instruction-migration?agent_id='+agent)).data;}catch(error){return {disk_state:'unknown',session_state:'unknown',findings:[],reason:error.message};}}
async function openEdge(a,b,cursor=null){
 const request=++seq,mine=viewer;
 const j=await api('/api/edge-messages?view_as_agent_id='+mine+'&a_agent_id='+a+'&b_agent_id='+b+'&limit=20'+(cursor?'&cursor='+encodeURIComponent(cursor):''));
 const rows=await bodies(j.data.items,mine),finding=await instruction(mine);
 if(request!==seq||mine!==viewer)return;conversationQuery=null;edge={a,b};notice=finding;
 const previous=cursor&&page?page.items:[];page={...j.data,items:[...previous,...rows].sort((x,y)=>x.id-y.id)};selected=cursor?selected:rows[0]?.id??null;draw();
}
function draw(){
 drawer.classList.add('on');drawer.setAttribute('aria-hidden','false');
 const participants=F.participants(page.items,edge);
 drawer.querySelector('.ed-a').textContent=participants[0];drawer.querySelector('.ed-b').textContent=participants[1];drawer.querySelector('.ed-cnt').textContent=page.items.length+' visible';
 const root=list;
 const options={selected,instruction:window.parent!==window&&new URLSearchParams(location.search).get('embed')==='1'?null:notice,onSelect:id=>selectMessage(id).catch(error=>say(error.message))};
 if(conversationQuery?.label!=null){
  root.replaceChildren();if(options.instruction&&F.needsNotice(options.instruction)&&!F.dismissed(options.instruction))root.append(F.notice(options.instruction));
  for(const group of page.groups){const section=E('section');section.append(E('p','gap-note','Saved label · source '+group.origin.source_provenance));const body=E('div');F.render(body,{...options,instruction:null,messages:group.rows,incomplete:group.history_incomplete});section.append(body);root.append(section);}
  if(page.next_cursor)root.append(B('Load more visible mail',()=>loadMore().catch(error=>say(error.message)),'reply-load'));
 }else F.render(root,{...options,messages:page.items,incomplete:page.items.some(m=>m.history_incomplete),next:page.next_cursor,onMore:()=>loadMore().catch(error=>say(error.message))});

}
async function selectMessage(id){
 if(id!=null&&!page.items.some(m=>m.id===id)){await conversation(id);return;}
 const box=list,pos=box.scrollTop;selected=id;draw();box.scrollTop=pos;if(id!=null)document.getElementById('reply-'+id)?.scrollIntoView({block:'nearest'});
}

async function conversation(id,cursor=null,label=null){
 const request=++seq,mine=viewer;
 const qs=new URLSearchParams({view_as_agent_id:mine,limit:20,...(label!=null?{label}:{message_id:id}),...(cursor?{cursor}:{})});
 const j=await api('/api/mail/'+(label!=null?'legacy-label':'conversation')+'?'+qs);
 const groups=[];for(const group of j.data.items){const rows=await bodies(group.source_message_ids.map(id=>({id,body_md:null})),mine);groups.push({...group,rows});}
 const finding=await instruction(mine);if(request!==seq||mine!==viewer)return;
 // Legacy label groups retain source boundaries; never infer reply edges from labels.
 edge=null;conversationQuery={id,label};notice=finding;const previous=cursor&&page?page.groups||[]:[];const merged=[...previous];for(const group of groups){const prior=merged.find(x=>JSON.stringify(x.origin)===JSON.stringify(group.origin));if(prior)prior.rows.push(...group.rows);else merged.push(group);}for(const group of merged)group.rows.sort((a,b)=>a.id-b.id);page={...j.data,items:merged.flatMap(g=>g.rows),groups:merged};selected=id;
 draw();
}
async function loadMore(){if(edge)return openEdge(edge.a,edge.b,page.next_cursor);if(conversationQuery)return conversation(conversationQuery.id,page.next_cursor,conversationQuery.label);}

return Object.freeze({setViewer(id){if(id!==viewer){clear();viewer=id;}},openEdge,openMessage:conversation,openLegacyLabel:label=>conversation(null,null,label),loadMore,loadBody:async id=>(await api('/api/mail/message?view_as_agent_id='+viewer+'&id='+id)).data,instruction,clear,refreshNotice:async()=>{if(viewer&&page){const mine=viewer,n=seq,row=await instruction(mine);if(mine===viewer&&n===seq){notice=row;draw();}}},get page(){return page;}});
}
window.OrreryMailController=Object.freeze({version:1,create});
})();
