/* Shared lookup, intent replay, status actions and operator form. */
(function(){'use strict';
function create({session,showDialog,closeDialog,errorAt,getDialog,refresh=async()=>{}}){
const F=window.OrreryMailFlow,E=F.element,B=F.button,api=session.api,stored=session.stored,loadRoster=refresh;
function recentDirs(){try{const a=JSON.parse(stored('localStorage','getItem','agentdash.spawn.recent-dirs')||'[]');return Array.isArray(a)?a.filter(x=>typeof x==='string').slice(0,10):[];}catch(_){return [];}}
const oldDir=stored('localStorage','getItem','agentdash.spawn.dir');if(oldDir){const a=recentDirs();if(!a.includes(oldDir))a.unshift(oldDir);stored('localStorage','setItem','agentdash.spawn.recent-dirs',JSON.stringify(a.slice(0,10)));stored('localStorage','removeItem','agentdash.spawn.dir');}
function open(){
 const form=showDialog('Launch an agent');
 const fields={client_name:'Client slot (new identity needs a new slot)',name:'Agent name',model:'Model',cwd:'Work folder'};
 for(const [name,title] of Object.entries(fields)){const label=E('label','',title),input=E('input');input.name=name;input.required=true;if(name==='cwd'){input.setAttribute('list','global-recent-dirs');input.value=recentDirs()[0]||'';}label.append(input);form.append(label);}
 const choices=E('datalist');choices.id='global-recent-dirs';for(const path of recentDirs()){const o=E('option');o.value=path;choices.append(o);}form.append(choices);
 const label=E('label','','Engine'),provider=E('select');provider.name='program';for(const [v,t] of [['claude-code','Claude'],['codex','Codex'],['antigravity','Gemini'],['daemon','Daemon']]){const o=E('option','',t);o.value=v;provider.append(o);}label.append(provider);form.append(label,E('p','','Existing names keep their identity. Configuration or trust may need an explicit operator step. No credentials are issued by a resume.'));
 const folderResult=E('div');form.append(B('Browse folders',async()=>{try{const row=await api('/api/fs/dirs?path='+encodeURIComponent(form.elements.cwd.value));folderResult.replaceChildren();if(row.path){form.elements.cwd.value=row.path;folderResult.append(E('p','',row.path));for(const d of row.dirs)folderResult.append(B(d.name,()=>{form.elements.cwd.value=d.path;},'reply-load'));}else folderResult.append(E('p','','Folder is outside the configured roots or unavailable.'));}catch(error){errorAt(form,error);}}),folderResult);
 const result=E('div');form.append(B('Cancel',closeDialog),B('Spawn',()=>form.requestSubmit()),result);
 form.onsubmit=async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(form));try{
  const lookup=await api('/api/global/clients?name='+encodeURIComponent(data.name));result.replaceChildren();
  if(lookup.matches.length){for(const m of lookup.matches){const current=await api('/api/global/spawn-status?client_name='+encodeURIComponent(m.client_name));showOperation(result,current,{...data,client_name:m.client_name},lookup.matches.length>1);}return;}
  const r=await api('/api/global/spawn',data);const paths=recentDirs().filter(x=>x!==data.cwd);paths.unshift(data.cwd);stored('localStorage','setItem','agentdash.spawn.recent-dirs',JSON.stringify(paths.slice(0,10)));showOperation(result,r,data);await loadRoster();
 }catch(error){errorAt(form,error);if(session.unlocked){try{const current=await api('/api/global/spawn-status?client_name='+encodeURIComponent(data.client_name));showOperation(result,current,data);}catch(_){/* Original refusal remains visible. */}}}};form.querySelector('input').focus();
 (async()=>{const input=form.elements.cwd,initial=input.value;let chosen=null;for(const path of recentDirs()){const row=await api('/api/fs/dirs?path='+encodeURIComponent(path));if(row.path){chosen=row.path;break;}}if(!chosen)chosen=(await api('/api/fs/dirs')).path;if(getDialog()?.contains(input)&&input.value===initial)input.value=chosen||'';})().catch(error=>errorAt(form,error));
}
function showOperation(root,row,intent=null,append=false){
 if(!append)root.replaceChildren();root.append(E('p','',row.client_name+' · '+row.stage+' · '+(row.reason||'')));
 const execute=async(op)=>{try{
  const r=op==='resume'?await api('/api/global/resume',{client_name:row.client_name,open:true}):await api('/api/global/spawn',{...intent,...(row.registration_sha256?{expected_digest:row.registration_sha256}:{})});
  showOperation(root,r,intent);await loadRoster();
 }catch(error){root.append(E('p','global-error',error.message));}};
 for(const op of row.next_operations||[]){
  if((['replay-registration','finalize-registration','launch'].includes(op)||(op==='configure-provider'&&row.reason==='INSTRUCTION_PROVIDER_CONFIG_REQUIRED'))&&intent)root.append(B(op,()=>execute(op)));
  else if(op==='configure-provider'&&['CODEX_HOOK_CONFIGURE_REQUIRED','PERSISTENT_EXECUTION_REQUIRED'].includes(row.reason)){
   const codex=row.reason==='CODEX_HOOK_CONFIGURE_REQUIRED';
   root.append(E('p','',codex?"Set AGENTSTACK_CLIENT_CONFIG to this slot’s runtime-client.json. Run codex-plan, review and explicitly approve the plan, then run codex-apply. Launch never approves trust.":"Set AGENTSTACK_CLIENT_CONFIG to this slot’s runtime-client.json. If its fixed profile.json is absent, run agentstack-runtime-client save-profile first. Then run agentstack-persistent configure --operator before launch."));
   const details=E('p','','Details: ');
   for(const [label,file,anchor] of [['English','global-delivery-client.en.md',codex?'codex-hook-trust':'persistent-execution'],['日本語','global-delivery-client.md',codex?'codex-hook-trust':'persistent-execution']]){const a=E('a','',label);a.href='https://github.com/gyroid-eth/orrery-telemetry/blob/master/docs/'+file+'#'+anchor;a.target='_blank';a.rel='noopener';details.append(a,' ');}root.append(details);
  }
  else if(op==='launch-status')root.append(B('Resume '+row.client_name,()=>execute('resume')));
  else if(op==='registration-abandon'&&row.registration_sha256)root.append(B('Abandon registration attempt',async()=>{if(!confirm('Abandon this local attempt? The server identity will remain.'))return;try{await api('/api/global/registration-abandon',{client_name:row.client_name,expected_digest:row.registration_sha256});root.replaceChildren(E('p','','Local pending attempt abandoned.'));}catch(error){root.append(E('p','global-error',error.message));}}));
  else root.append(E('p','',op));
 }
}

return Object.freeze({open,showOperation,lookup:name=>api('/api/global/clients?name='+encodeURIComponent(name)),status:clientName=>api('/api/global/spawn-status?client_name='+encodeURIComponent(clientName)),submit:intent=>api('/api/global/spawn',intent),clear:closeDialog});
}
window.OrreryOperationController=Object.freeze({version:1,create});
})();
