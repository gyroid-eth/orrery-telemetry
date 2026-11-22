/* Shared read-only reply view. Input is the server's visibility projection. */
(function(){
'use strict';
function element(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=String(text);return e;}
function button(text,fn,cls){const e=element('button',cls,text);e.type='button';e.onclick=fn;return e;}
function link(text,url){const e=element('a','',text);e.href=url;e.target='_blank';e.rel='noopener';return e;}
function details(parent){parent.append(' Details: ',link('English','https://github.com/gyroid-eth/orrery-telemetry/blob/master/docs/instruction-migration.en.md#legacy-blocks'),' / ',link('日本語','https://github.com/gyroid-eth/orrery-telemetry/blob/master/docs/instruction-migration.md#legacy-blocks'));}
function fingerprint(row){return JSON.stringify([row.provider,row.target_path,row.disk_state,row.session_state,(row.findings||[]).map(f=>[f.source_path,f.kind,f.block_sha256,f.template_id])]);}
function dismissed(row){try{return sessionStorage.getItem('orrery.instruction-dismiss')===fingerprint(row);}catch(_){return false;}}
function notice(row,{force=false}={}){
 const box=element('div','instruction-hint');
 const text=row.session_state==='reload_required'?'Instructions changed. Restart this session.':row.disk_state==='unknown'?'Instruction status could not be verified.':row.disk_state==='needs_review'?'ORRERY instructions need review.':row.disk_state==='clean'?'Current ORRERY instructions found.':row.disk_state==='not_configured'?'ORRERY instructions are not configured.':'Old ORRERY instructions remain.';
 box.append(element('span','',text));details(box);
 for(const f of row.findings||[]){
  if(f.kind==='missing'||f.kind==='current')continue;
  const path=element('div','hint-path',f.source_path);box.append(path);
  if(f.cleanup_argv?.length){
   const command=f.cleanup_argv.map(x=>"'"+x.replaceAll("'", "'\\''")+"'").join(' ');
   const output=element('code','hint-command',command);output.hidden=true;
   box.append(' ',button('Copy cleanup command',async()=>{output.hidden=false;try{await navigator.clipboard.writeText(command);}catch(_){/* Selectable literal remains visible. */}},'reply-link'),output);
  }
 }
 if(row.reason)box.append(element('div','hint-path',row.reason));
 box.append(element('div','hint-path','Disk candidates only; this does not prove what the running session loaded.'));
 if(!force)box.append(button('Dismiss for this page session',()=>{try{sessionStorage.setItem('orrery.instruction-dismiss',fingerprint(row));}catch(_){}box.remove();},'reply-link'));
 return box;
}
function needsNotice(row){if(row.reason==='PROVIDER_HOME_UNPROVEN')return false;return row.session_state==='reload_required'||!['clean','not_configured'].includes(row.disk_state);}
function participants(messages,edge=null){
 const names=new Map();for(const m of messages)for(const a of [m.sender,...(m.to||[]),...(m.cc||[]),...(m.bcc||[])])if(a&&typeof a.name==='string'&&a.name)names.set(a.agent_id,a.name);
 const ids=edge?[edge.a,edge.b]:[...names.keys()];
 return [names.get(ids[0])||'Unavailable participant',ids.length>2?'Multiple participants':names.get(ids[1])||'Unavailable participant'];
}
function render(root,{messages=[],selected=null,incomplete=false,next=null,onSelect=()=>{},onMore=null,instruction=null}={}){
 root.replaceChildren();root.classList.add('reply-view');
 if(instruction&&needsNotice(instruction)&&!dismissed(instruction))root.append(notice(instruction));
 if(incomplete)root.append(element('p','gap-note','Earlier mail is unavailable in this view.'));
 if(!messages.length)root.append(element('p','gap-note','No visible mail in this view.'));
 const flow=element('div','reply-flow');root.append(flow);
 for(const m of messages){
  const item=element('article','reply-item'+(m.reply_to!=null?' reply':'')+(selected===m.id?' selected':''));item.id='reply-'+m.id;
  const meta=element('div','reply-meta');meta.append(element('span','sender',m.sender.name),' → ',element('span','', [...m.to,...m.cc,...m.bcc].map(x=>x.name).join(', ')),element('time','',m.created_ts.replace('T',' ').replace(/:\d\d(?:\.\d+)?(?:Z|[+-].*)$/,'')));item.append(meta);
  const reference=element('div','reply-to');
  if(m.reply_to!=null)reference.append(button('↳ Reply to #'+m.reply_to,()=>onSelect(m.reply_to),'reply-link'));
  else reference.textContent='#'+m.id+' · visible conversation starts here';
  item.append(reference,element('h3','reply-subject',m.subject||'(no subject)'),element('div','reply-body',m.body_md??'Body not loaded.'));
  const more=element('div','reply-more');more.append(button(selected===m.id?'Collapse message':'Read message',()=>onSelect(selected===m.id?null:m.id),'reply-link'));item.append(more);flow.append(item);
 }
 if(next&&onMore)root.append(button('Load more visible mail',onMore,'reply-load'));
}
window.OrreryMailFlow=Object.freeze({participants,render,notice,element,button,details,fingerprint,needsNotice,dismissed});
})();
