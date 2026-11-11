/* Opt-in, read-only conversation history. Text enters the DOM through
   textContent only; inert Markdown uses the bundled renderer. A file preview is text through
   textContent or a PNG the relay checked, shown from a data:image/png URL that
   this file builds from validated base64; nothing else sets a source. Save
   writes exactly what the preview shows to the device through one object URL,
   revoked on close. Nothing is kept in storage or URLs; unmount clears the
   DOM, the preview, requests, timers and pending rows. */
(()=>{'use strict';
const make=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!==undefined)e.textContent=txt;return e;};
const MAX_BYTES=768*1024,MAX_KEPT=200,POLL=5000;
const MAX_PREVIEW_BYTES=6*1024*1024,MAX_SIDE=8192,MAX_PIXELS=8000000,REF=/^[a-f0-9]{32}$/,BASE64=/^(?:[A-Za-z0-9+/]{4})+(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
let capability=null,gen=0,timer=0,name='',root=null,list=null,status=null,olderButton=null,retryButton=null;
/* Longer than the relay's 15-second wait for cockpit, so its own answer arrives first. */
const WAIT=20000;
/* Poll and Show older each own a controller, so neither aborts the other. */
const controllers={poll:null,older:null,preview:null};
let panel=null,saveUrl='',previewFocus=null,previewRequest=null,previewRetry=false;
let events=[],seen=new Set(),older=null,pending=[],failures=0,loadingOlder=false,following=true,savedTop=0;
let mailResize=null,mailWidth=0;
let toolGroups=[],toolGroupFor=new Map(),toolSerial=0;
const thumbnails=new Map();let thumbnailObserver=null;
const messages={
 history_unconfirmed:'This agent’s record is not verified as its own, so history stays hidden.',
 history_stale:'The verified record predates this launch, so history stays hidden.',
 agent_unavailable:'This agent is not in the current record list.',
 provider_unavailable:'History is not available for this kind of agent.',
 dashboard_upgrade_required:'Update Telemetry to read this agent’s history.',
 claude_next_phase:'History is not available for this agent yet.',
 invalid_binding:'The record could not be verified.',
 history_unreadable:'The record could not be read.',
 record_too_large:'The latest record is too large to show.',
 page_expired:'Older page expired. Showing the latest page.',
 history_slow:'Taking longer than usual: the computer is busy. Retrying, or tap Retry.',
 older_slow:'Older messages are taking longer than usual. Tap Show older again.',
 omitted:'Some messages are omitted here: too long to show or not loadable.',
 kept_limit:'Showing the latest 200 messages. Older messages are not loaded.',
};
const previewMessages={
 preview_expired:'This preview link expired. Reopen the conversation and tap the file again.',
 preview_too_large:'Too large to preview on the phone.',
 preview_type_denied:'This kind of file cannot be previewed.',
 preview_path_denied:'Not previewable: outside the agent’s folders.',
 preview_changed:'The file changed while it was read. Tap again.',
 history_slow:'Taking longer than usual: the computer is busy. Tap the file again.',
};
async function enabled(){
 if(capability!==null)return capability;
 try{
  const r=await fetch('/view/history',{credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer'});
  const v=r.ok?await r.json():null;capability=!!(v&&v.enabled===true);
 }catch(_){capability=false;}
 return capability;
}
async function request(token,slot){
 controllers[slot]?.abort();const c=new AbortController();controllers[slot]=c;let late=false;const t=setTimeout(()=>{late=true;c.abort();},WAIT);
 try{
  const r=await fetch('/view/conversation?name='+encodeURIComponent(name)+(token?'&page='+token:''),
   {credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',signal:c.signal});
  const raw=await r.text();if(new TextEncoder().encode(raw).length>MAX_BYTES)throw Error('unavailable');
  const v=JSON.parse(raw);if(r.status===410)return {ok:false,error:'page_expired'};
  if(!v||typeof v!=='object')throw Error('unavailable');
  if(v.ok===true&&(v.agent!==name||!Array.isArray(v.events)||v.events.length>40))throw Error('unavailable');
  return v;
 }catch(e){
  // A request replaced by a newer one of the same kind is not a failure;
  // the newer one owns the retry timer.
  if(c.signal.aborted&&!late)e.superseded=true;if(late)e.slow=true;throw e;
 }finally{clearTimeout(t);if(controllers[slot]===c)controllers[slot]=null;}
}
function clock(ts){const d=new Date(ts);return Number.isFinite(d.getTime())?d.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}):'';}
const markdown=()=>window.OrreryConversationMarkdown;
function paintSpeaker(head){
 const recorded=head.dataset.model||'',profile=window.OrreryMobileProfile?.forName(name,recorded),model=recorded||profile?.model||'';
 const key=JSON.stringify([name,model,profile?.provider,profile?.locality]);if(head.dataset.profile===key)return;head.dataset.profile=key;
 const face=window.OrreryMobileProfile?.face(name,recorded);head.querySelector('.conversation-avatar')?.remove();
 if(face){face.classList.add('conversation-avatar');head.prepend(face);}
 head.querySelector('.conversation-speaker-name').textContent=name;
}
function speaker(e){
 const head=make('div','conversation-byline');head.dataset.model=typeof e.model==='string'?e.model:'';
 head.append(make('span','conversation-speaker-name'));
 const at=clock(e.ts);if(at){const time=make('time','conversation-speaker-time',at);time.dateTime=e.ts;head.append(time);}
 paintSpeaker(head);return head;
}
function refreshProfiles(){
 if(!list)return;
 let mailChanged=false;
 list.querySelectorAll('.conversation-byline').forEach(paintSpeaker);
 for(const mark of list.querySelectorAll('.conversation-mail-mark')){
  const peer=mark.closest?.('.conversation-mail-peer');if(peer)peer.disabled=!window.OrreryMobileProfile?.forName(mark.dataset.peerName);
  const p=window.OrreryMobileProfile?.forName(mark.dataset.peerName),key=JSON.stringify(p)||'';if(mark.dataset.profile===key)continue;mark.dataset.profile=key;mailChanged=true;
  const face=p&&window.OrreryMobileProfile?.face(mark.dataset.peerName);mark.replaceChildren();mark.hidden=!face;if(face){face.classList.add('conversation-avatar');mark.append(face);}
 }
 if(mailChanged)layoutMailRoutes();
}
function peerWidth(peer){
 const mark=peer.querySelector('.conversation-mail-mark'),label=peer.querySelector('.conversation-mail-peer-name');
 return label.scrollWidth+(mark.hidden?0:mark.getBoundingClientRect().width+5);
}
function layoutMailThread(card){
 const line=card.querySelector('.conversation-mail-thread'),avatar=card.querySelector('.conversation-mail-sender .conversation-avatar');
 if(card.getAttribute('aria-expanded')!=='true'||!avatar){
  if(!line.hidden)line.hidden=true;
  if(card.classList.contains('is-threaded'))card.classList.remove('is-threaded');
  return;
 }
 // Keep the body's inset while measuring: removing it can clamp scrollTop.
 const box=card.getBoundingClientRect(),face=avatar.getBoundingClientRect();
 if(!face.width)return;
 const x=face.left+face.width/2-box.left,inset=(x+16)+'px',left=(x-.5)+'px',top=(face.bottom-box.top+3)+'px';
 if(card.style.getPropertyValue('--mail-thread-inset')!==inset)card.style.setProperty('--mail-thread-inset',inset);
 if(line.style.left!==left)line.style.left=left;
 if(line.style.top!==top)line.style.top=top;
 if(!card.classList.contains('is-threaded'))card.classList.add('is-threaded');
 if(line.hidden)line.hidden=false;
}
function layoutMailRoutes(){
 if(!list?.isConnected)return;
 for(const route of list.querySelectorAll('.conversation-mail-route')){
  const recipients=route.querySelector('.conversation-mail-recipients'),peers=[...recipients.querySelectorAll('.conversation-mail-peer')],more=recipients.querySelector('.conversation-mail-more');
  recipients.dataset.mode=peers.length===1?'single':'names';peers.forEach(p=>p.hidden=false);more.hidden=true;more.style.marginLeft='0';
  if(peers.length<=1)continue;
  const sender=route.querySelector('.conversation-mail-sender'),fixed=peerWidth(sender)+route.querySelector('.conversation-mail-chevron').getBoundingClientRect().width+route.querySelector('.conversation-mail-arrow').getBoundingClientRect().width+18;
  const available=route.clientWidth-fixed,needed=peers.reduce((sum,p)=>sum+peerWidth(p),0)+(peers.length-1)*6;
  if(needed<=available)continue;
  recipients.dataset.mode='icons';
  const known=peers.filter(p=>!p.querySelector('.conversation-mail-mark').hidden),budget=Math.max(0,available);
  let visible=known.length;
  // Keep the sender's full name before spending width on more recipient icons.
  for(;;){
   const omitted=peers.length-visible;more.textContent=omitted?'+'+omitted:'';more.hidden=!omitted;
   const width=(visible?18+(visible-1)*14:0)+(omitted?(visible?6:0)+more.scrollWidth:0);
   if(width<=budget||visible===0)break;visible--;
  }
  const shown=new Set(known.slice(0,visible));peers.forEach(p=>p.hidden=!shown.has(p));
  more.style.marginLeft=visible?'6px':'0';
 }
 for(const card of list.querySelectorAll('.conversation-mail'))layoutMailThread(card);
}
function body(text,assistant=false){
 const box=make('div','conversation-text'),m=markdown();
 if(m&&assistant)m.renderDocument(text,box,{codeControls:true});
 else if(m)m.renderText(text,box);else box.textContent=text;
 return box;
}
function mailCard(m,ts){
 const card=make('div','conversation-mail '+m.direction);
 const head=make('div','conversation-mail-head'),route=make('div','conversation-mail-route');
 const peer=(value,strong)=>{const p=make('button','conversation-mail-peer'+(strong?' is-counterpart':'')),mark=make('span','conversation-mail-mark');p.type='button';mark.dataset.peerName=value||'';mark.hidden=true;p.title=value||'unknown';p.setAttribute('aria-label','Open '+(value||'unknown')+' conversation');p.disabled=!window.OrreryMobileProfile?.forName(value);p.onclick=e=>{e.stopPropagation();if(value&&value!==name)window.OrreryMobileTerminal?.open(value,window.OrreryMobileIdentity?.forName(value),'conversation');};p.append(mark,make('span','conversation-mail-peer-name',value||'unknown'));return p;};
 const sender=peer(m.sender||(m.direction==='sent'?name:'recorded sender unavailable'),m.direction==='received');sender.classList.add('conversation-mail-sender');
 const targets=m.recipients.length?m.recipients:[m.direction==='received'?name:'recorded recipient unavailable'];
 const recipients=make('span','conversation-mail-recipients');recipients.title=targets.join(', ');recipients.setAttribute('role','group');recipients.setAttribute('aria-label','Recipients: '+targets.join(', '));
 for(const recipient of targets)recipients.append(peer(recipient,m.direction==='sent'));
 const more=make('span','conversation-mail-more');more.hidden=true;recipients.append(more);
 route.append(make('span','conversation-mail-chevron','▸'),sender,make('span','conversation-mail-arrow','→'),recipients);
 head.append(route);const at=clock(ts);if(at){const time=make('time','conversation-mail-time',at);time.dateTime=ts;head.append(time);}
 const subject=make('span','conversation-mail-subject-row');
 if(['high','urgent'].includes(m.importance)){const dot=make('span','conversation-mail-importance');dot.dataset.importance=m.importance;dot.title=dot.ariaLabel=m.importance==='high'?'High importance':'Urgent importance';dot.setAttribute('role','img');subject.append(dot);}
 subject.append(make('strong','conversation-mail-subject',m.subject||'(no subject)'));
 card.append(make('div','conversation-mail-label','Mail · '+(m.direction==='received'?'Received':'Sent')),head);
 const detail=make('pre','conversation-mail-body',m.body);detail.hidden=true;
 const note=make('small','conversation-mail-note',m.coverage==='preview'?'Recorded preview only':m.coverage==='unavailable'?'Body not in the record':m.truncated?'Shortened':'Tap to show the recorded body');
 const line=make('span','conversation-mail-thread');line.hidden=true;line.setAttribute('aria-hidden','true');
 const toggle=make('button','conversation-mail-toggle');toggle.type='button';toggle.append(subject,note);toggle.setAttribute('aria-expanded','false');
 card.append(toggle,detail,line);card.setAttribute('aria-expanded','false');
 const closedNote=note.textContent;card.onclick=()=>{detail.hidden=!detail.hidden;card.setAttribute('aria-expanded',String(!detail.hidden));toggle.setAttribute('aria-expanded',String(!detail.hidden));card.querySelector('.conversation-mail-chevron').textContent=detail.hidden?'▸':'▾';note.textContent=detail.hidden?closedNote:'Recorded body · tap to hide';layoutMailThread(card);};
 return card;
}
// The cockpit puts file references on the file tool calls (Read, Write,
// Edit, apply_patch, SendUserFile...), so a tool row carries chips too.
function files(e,r){if(Array.isArray(e.files)&&e.files.length){const f=make('div','conversation-files');e.files.forEach(x=>f.append(chip(x)));r.append(f);}}
function row(e){
 if(e.kind==='tool'){const r=make('div','conversation-tool',e.text);r.dataset.id=e.id;files(e,r);if(Array.isArray(e.mail))e.mail.forEach(m=>r.append(mailCard(m,e.ts)));return r;}
 if(e.kind==='mail'){const r=make('div','conversation-mails');r.dataset.id=e.id;e.mail.forEach(m=>r.append(mailCard(m,e.ts)));return r;}
 const command=e.role==='user'&&markdown()?.commandRecord(e.text);
 const r=make('div',command?.shell?'conversation-shell':'conversation-bubble '+(e.role==='user'?'user':'assistant'));r.dataset.id=e.id;
 if(command){if(command.shell)r.append(make('small','conversation-speaker','SHELL'));markdown().renderCommand(e.text,r);}else{if(e.role==='assistant')r.append(speaker(e));r.append(body(e.text,e.role==='assistant'));}

 if(Array.isArray(e.mail))e.mail.forEach(m=>r.append(mailCard(m,e.ts)));
 files(e,r);
 const meta=[e.role==='assistant'&&!command?'':clock(e.ts),e.truncated?'shortened':''].filter(Boolean).join(' · ');if(meta)r.append(make('small','conversation-time',meta));
 return r;
}
function chip(x){
 const label=(x.kind==='image'?'Image · ':'File · ')+x.label;
 if(typeof x.ref!=='string'||!REF.test(x.ref))return make('span','conversation-chip '+x.kind,label);
 const b=make('button','conversation-chip '+x.kind+' previewable',label);b.type='button';
 b.setAttribute('aria-label','Preview '+x.label);b.onclick=()=>openPreview(x);return b;
}
/* 表示済み画像は tool を閉じても会話に残す。人の入力画像は吹き出しと
   同じ側に置き、参照と画像は既存の preview の許可を使う。 */
function shownFiles(e){
 if(e.shown!==true||!Array.isArray(e.files)||!e.files.length)return null;
 const box=make('div','conversation-shown-files');box.dataset.shownFor=e.id;
 box.dataset.role=e.role==='user'?'user':'assistant';
 for(const x of e.files){
  if(x.kind!=='image'||typeof x.ref!=='string'||!REF.test(x.ref)){
   box.append(make('span','conversation-chip',x.label));continue;
  }
  const button=make('button','conversation-thumbnail');button.type='button';button.setAttribute('aria-label','画像を拡大: '+x.label);
  const image=make('img','conversation-thumbnail-image');image.alt=x.label;image.hidden=true;
  const hint=make('small','conversation-thumbnail-status','画像を読み込み中…');
  button.append(image,make('span','conversation-thumbnail-label',x.label),hint);button.onclick=()=>openPreview(x);box.append(button);
  const item={button,image,hint,x,state:'waiting',visible:typeof IntersectionObserver!=='function',controller:null};thumbnails.set(button,item);
  if(typeof IntersectionObserver==='function'){
   if(!thumbnailObserver)thumbnailObserver=new IntersectionObserver(entries=>{
    for(const entry of entries){const item=thumbnails.get(entry.target);if(item)item.visible=entry.isIntersecting;}
    loadThumbnails();
   },{root:list,rootMargin:'160px'});
   thumbnailObserver.observe(button);
  }
 }
 return box;
}
function forgetThumbnails(node){
 for(const [button,item] of thumbnails)if(!node||node.contains(button)){
  item.controller?.abort();thumbnailObserver?.unobserve(button);clearImage(item.image);thumbnails.delete(button);
 }
 if(!node){thumbnailObserver?.disconnect();thumbnailObserver=null;}
}
function clearImage(img){img.src='';}
function paintImage(img,v){img.src=pngSource(v);}
function loadThumbnails(){
 if(!root||document.hidden)return;
 let active=[...thumbnails.values()].filter(x=>x.state==='loading').length;
 for(const item of thumbnails.values()){
  if(active>=2)break;
  if(item.state!=='waiting'||!item.visible||!item.button.isConnected)continue;
  active++;loadThumbnail(item);
 }
}
async function loadThumbnail(item){
 const g=gen,c=new AbortController();item.controller=c;item.state='loading';
 const t=setTimeout(()=>c.abort(),WAIT);let value=null;
 try{
  const response=await fetch('/view/conversation/preview?name='+encodeURIComponent(name)+'&ref='+item.x.ref+'&thumbnail=1',
   {credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',signal:c.signal});
  const raw=await response.text();if(raw.length>MAX_PREVIEW_BYTES)throw Error('unavailable');
  if(response.ok)value=JSON.parse(raw);
 }catch(_){}finally{clearTimeout(t);item.controller=null;}
 if(!thumbnails.has(item.button))return;
 if(g!==gen||document.hidden){item.state='waiting';loadThumbnails();return;}
 if(value?.ok===true&&pngSource(value)){
  paintImage(item.image,value);item.image.hidden=false;item.hint.textContent='タップして拡大';item.state='done';
 }else{item.hint.textContent='画像を取得できません。タップして開く';item.state='failed';}
 // Keep only a small number of decoded thumbnails as the reader scrolls.
 const loaded=[...thumbnails.values()].filter(x=>x.state==='done');
 for(const old of loaded){if(loaded.filter(x=>x.state==='done').length<=24)break;if(!old.visible){clearImage(old.image);old.image.hidden=true;old.state='waiting';}}
 loadThumbnails();
}
/* Checked again here: a PNG data URL from strict base64 within the relay's
   side and pixel limits. */
function pngSource(v){
 return v&&v.kind==='image'&&(v.mime===undefined||v.mime==='image/png')&&typeof v.data==='string'&&v.data.length<=MAX_PREVIEW_BYTES&&BASE64.test(v.data)&&v.data.startsWith('iVBORw0KGgo')
  &&Number.isInteger(v.width)&&Number.isInteger(v.height)&&v.width>0&&v.height>0&&v.width<=MAX_SIDE&&v.height<=MAX_SIDE&&v.width*v.height<=MAX_PIXELS
  ?'data:image/png;base64,'+v.data:null;
}
function closePreview(restoreFocus=false){
 controllers.preview?.abort();controllers.preview=null;dropSave();
 previewRequest=null;previewRetry=false;
 if(panel){for(const img of panel.querySelectorAll('img'))clearImage(img);panel.replaceChildren();panel.remove();}
 panel=null;
 if(restoreFocus&&previewFocus?.isConnected)previewFocus.focus?.({preventScroll:true});previewFocus=null;
}
function dismissPreview(){if(!panel)return false;closePreview(true);return true;}
/* A plain file name: the last path part, without control or bidi-override
   characters, at most 120 characters. Images are saved as the PNG cockpit
   re-drew, so they end in .png; text that a viewer could render as markup
   (.html, .htm, .xhtml, .svg, .xml) gets .txt, matching the plain-text bytes. */
function fileName(label,image){
 let n=String(label||'').split(/[\\/]/).pop().replace(/[\u0000-\u001f\u007f-\u009f\u200e\u200f\u202a-\u202e\u2066-\u2069]/g,'').trim().slice(0,120);
 if(!n||n==='.'||n==='..')n=image?'image':'file.txt';
 if(image)return /\.png$/i.test(n)?n:n+'.png';
 return /\.(?:html?|xhtml|svg|xml)$/i.test(n)?n+'.txt':n;
}
function dropSave(){if(saveUrl){URL.revokeObjectURL(saveUrl);saveUrl='';}}
/* Saves the bytes this preview already holds; no further request is made. */
function saveButton(head,close,label,blob){
 const b=make('button','conversation-preview-save','Save');b.type='button';
 b.onclick=()=>{
  if(!panel)return;dropSave();saveUrl=URL.createObjectURL(blob());
  const a=make('a');a.download=fileName(label,blob.image);a.href=saveUrl;a.hidden=true;
  panel.append(a);a.click();a.remove();
 };
 head.insertBefore(b,close);
}
function pngBytes(data){const raw=atob(data),out=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)out[i]=raw.charCodeAt(i);return out;}
async function openPreview(x){
 closePreview();if(!root)return;
 previewRequest=x;
 previewFocus=document.activeElement;
 const g=gen,box=make('div','conversation-preview');panel=box;box.setAttribute('role','dialog');box.setAttribute('aria-label','Preview of '+x.label);
 const head=make('div','conversation-preview-head'),close=make('button','conversation-preview-close','Close');close.type='button';close.onclick=()=>closePreview(true);
 head.append(make('strong','',x.label),close);
 const state=make('p','conversation-preview-status','Loading preview…');state.setAttribute('role','status');
 const view=make('div','conversation-preview-body');box.append(head,state,view);root.append(box);
 close.focus?.({preventScroll:true});
 const c=new AbortController();controllers.preview=c;let late=false;const t=setTimeout(()=>{late=true;c.abort();},WAIT);
 let v=null,status=0;
 try{
  const r=await fetch('/view/conversation/preview?name='+encodeURIComponent(name)+'&ref='+x.ref,
   {credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',signal:c.signal});
  status=r.status;const raw=await r.text();if(raw.length>MAX_PREVIEW_BYTES)throw Error('unavailable');v=JSON.parse(raw);
 }catch(_){v=late?{ok:false,error:'history_slow'}:null;}
 finally{clearTimeout(t);if(controllers.preview===c)controllers.preview=null;}
 if(g!==gen||panel!==box)return;
 previewRequest=null;
 if(status===410)v={ok:false,error:'preview_expired'};
 if(v?.ok===true&&v.kind==='text'&&typeof v.text==='string'){
  state.textContent=v.truncated===true?'Showing the start of the file.':'';
  view.append(make('pre','conversation-preview-text',v.text));
  const text=v.text;saveButton(head,close,x.label,()=>new Blob([text],{type:'text/plain;charset=utf-8'}));
 }else if(v?.ok===true&&pngSource(v)){
  const img=make('img','conversation-preview-image');img.alt=x.label;img.width=v.width;img.height=v.height;
  paintImage(img,v);state.textContent='';view.append(img);
  const data=v.data,blob=()=>new Blob([pngBytes(data)],{type:'image/png'});blob.image=true;saveButton(head,close,x.label,blob);
 }else{
  state.textContent=previewMessages[v?.error]||messages[v?.error]||'Preview unavailable right now.';return;
 }
 if(typeof v.notice==='string'&&v.notice)box.append(make('small','conversation-preview-notice',v.notice));
}
function atBottom(){return !list||(list.isConnected?list.scrollHeight-list.clientHeight-list.scrollTop<=24:following);}
/* The sheet rebuilds its children; detaching resets scrollTop. Restore the
   reader's place (or the tail) after each reattachment. */
function restore(){if(!list||!list.isConnected)return;list.scrollTop=following?list.scrollHeight:savedTop;loadThumbnails();}
function setStatus(text){if(!status)return;status.textContent=text;
 // Routine explanations live in Information; failures remain on the surface.
 status.hidden=/^(?:Read-only record · updating|Latest record · updating|New messages below · paused while you read)$/.test(text);
}
function activity(){
 if(!list)return;list.querySelector('.conversation-activity')?.remove();
 const a=window.OrreryMobileAgent?.forName(name),box=make('div','conversation-activity');
 if(a?.running&&a.state==='work'&&!a.stale)box.textContent='Working'+(a.workDisplay?' · '+a.workDisplay:'');
 else{if(a?.running&&['ask','question'].includes(a.state)&&!a.stale){box.className+=' reply-needed';box.textContent=a.state==='ask'?'Reply needed · approval':'Reply needed · question';}else if(a?.stale)box.textContent='Activity observation unavailable';}
 if(box.textContent)list.append(box);
 const dot=document.querySelector?.('.reader-state');if(dot&&root?.isConnected){dot.dataset.state=a?.stale?'unknown':a?.state||'unknown';dot.title=a?.stale?'Activity observation unavailable':a?.state==='work'?'Working':a?.state==='ask'?'Approval needed':a?.state==='question'?'Reply needed':'Waiting';dot.setAttribute('aria-label',dot.title);}
}

function renderPending(){
 list?.querySelectorAll(':scope > .conversation-pending').forEach(x=>{if(x.contains?.(document.activeElement))list.focus?.({preventScroll:true});x.remove();});
 for(const p of pending){const r=make('div','conversation-bubble user conversation-pending');r.append(body(p.text),make('small','conversation-time','Sent to terminal · not yet in the record'));const dismiss=make('button','conversation-dismiss','Dismiss');dismiss.type='button';dismiss.onclick=()=>{list.tabIndex=-1;list.focus?.({preventScroll:true});pending=pending.filter(x=>x!==p);renderPending();activity();};r.append(dismiss);list.append(r);}
}
function settlePending(){
 const now=Date.now();
 pending=pending.filter(p=>now-p.at<180000&&!events.some(e=>!p.before.has(e.id)&&e.role==='user'&&e.kind==='text'&&(markdown()?.submissionText(e.text)||e.text).trim()===p.text.trim()));
}
function groupShellRows(){
 let owner=null;
 for(const e of events){
  const command=e.role==='user'&&markdown()?.commandRecord(e.text);
  if(!command?.shell){owner=null;continue;}
  const node=list.querySelector('[data-id="'+CSS.escape(e.id)+'"]');if(!node)continue;
  if(command.command!==null||!owner){owner=node;continue;}
  if(node.parentElement===owner)continue;
  const focus=document.activeElement,held=node.contains?.(focus);
  if(held)list.focus?.({preventScroll:true});
  node.className='conversation-shell-part';node.querySelector('.conversation-speaker')?.remove();owner.append(node);
  if(held&&focus.isConnected)focus.focus({preventScroll:true});
 }
}
function setToolGroupOpen(group,open){
 const top=group.button.getBoundingClientRect().top;
 group.open=open;group.button.setAttribute('aria-expanded',String(open));
 group.button.querySelector('.conversation-tool-arrow').textContent=open?'▾':'▸';
 for(const row of group.rows){if(!open&&row.contains?.(document.activeElement))group.button.focus?.({preventScroll:true});row.hidden=!open;}
 list.scrollTop+=group.button.getBoundingClientRect().top-top;
 following=atBottom();savedTop=list.scrollTop;
}
function regroupTools(focused=toolGroups.find(g=>g.button===document.activeElement)){
 const previous=toolGroups,used=new Set(),groups=[],byId=new Map(events.map(e=>[e.id,e]));let run=[];
 const finish=()=>{
  if(!run.length)return;
  const matches=[...new Set(run.map(r=>toolGroupFor.get(r.dataset.id)).filter(Boolean))];
  let group=matches.find(g=>g===focused&&!used.has(g))||matches.find(g=>!used.has(g));
  const open=matches.some(g=>g.open);
  if(!group){
   const button=make('button','conversation-tool-group');button.type='button';button.dataset.toolGroup=String(++toolSerial);
   const arrow=make('span','conversation-tool-arrow','▸');arrow.setAttribute('aria-hidden','true');
   button.append(arrow,make('span','conversation-tool-count'),make('span','conversation-tool-names'),make('span','conversation-tool-times'));
   group={button,open:false,rows:[]};button.onclick=()=>setToolGroupOpen(group,!group.open);
  }
  used.add(group);group.rows=run;group.open=open;
  const calls=run.map(r=>byId.get(r.dataset.id)),counts=new Map();
  for(const event of calls)counts.set(event.text,(counts.get(event.text)||0)+1);
  const names=[...counts].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])).map(([n,count])=>(n||'Tool')+' ×'+count).join(', ');
  const label=calls.length+' tool call'+(calls.length===1?'':'s'),dates=calls.map(e=>new Date(e.ts)).filter(d=>Number.isFinite(d.getTime())).sort((a,b)=>a-b);
  const times=dates.length?clock(dates[0])+(dates.length>1?'–'+clock(dates[dates.length-1]):''):'';
  const full=label+(names?' · '+names:'')+(dates.length?' · '+dates[0].toLocaleString()+(dates.length>1?' – '+dates[dates.length-1].toLocaleString():''):'');
  const button=group.button;button.title=full;button.setAttribute('aria-label',full);button.setAttribute('aria-expanded',String(open));
  button.querySelector('.conversation-tool-arrow').textContent=open?'▾':'▸';
  button.querySelector('.conversation-tool-count').textContent=label;
  button.querySelector('.conversation-tool-names').textContent=names?'· '+names:'';
  button.querySelector('.conversation-tool-times').textContent=times;
  if(button.nextElementSibling!==run[0])list.insertBefore(button,run[0]);
  for(const row of run)row.hidden=!open;
  groups.push(group);run=[];
 };
 for(const row of [...list.children]){
  if(row.className==='conversation-shown-files'){finish();continue;}
  if(row.dataset.id===undefined)continue;
  const e=byId.get(row.dataset.id);
  if(e?.kind==='tool'&&!e.mail?.length)run.push(row);else{finish();row.hidden=false;}
 }
 finish();
 for(const g of previous)if(!used.has(g)){if(g.button===document.activeElement)list.focus?.({preventScroll:true});g.button.remove();}
 toolGroups=groups;toolGroupFor.clear();for(const g of groups)for(const r of g.rows)toolGroupFor.set(r.dataset.id,g);
 if(focused&&(document.activeElement===document.body||document.activeElement===list))(groups.includes(focused)?focused.button:list).focus?.({preventScroll:true});
}
function clearToolGroups(){
 for(const g of toolGroups){if(g.button===document.activeElement)list?.focus?.({preventScroll:true});g.button.remove();}
 toolGroups=[];toolGroupFor.clear();toolSerial=0;
}
function dropEvent(id){
 const shown=list.querySelector('[data-shown-for="'+CSS.escape(id)+'"]');if(shown){forgetThumbnails(shown);shown.remove();}
 const node=list.querySelector('[data-id="'+CSS.escape(id)+'"]');if(!node)return;
 // A group can outlive its oldest member at the 200-event limit. Promote
 // retained output before removing that member, so its text is not lost.
 for(const part of [...node.children].filter(x=>x.dataset.id!==undefined)){
  part.className='conversation-shell';part.insertBefore(make('small','conversation-speaker','SHELL'),part.firstChild);node.parentNode.insertBefore(part,node);
 }
 if(node.contains?.(document.activeElement))list.focus?.({preventScroll:true});node.remove();
}
function append(page){
 const follow=atBottom(),fresh=page.filter(e=>!seen.has(e.id));
 for(const e of fresh){seen.add(e.id);events.push(e);const anchor=list.querySelector(':scope > .conversation-pending');list.insertBefore(row(e),anchor);const shown=shownFiles(e);if(shown)list.insertBefore(shown,anchor);}
 let dropped=false;
 while(events.length>MAX_KEPT){const gone=events.shift();seen.delete(gone.id);dropEvent(gone.id);older=null;dropped=true;}
 groupShellRows();
 regroupTools();
 refreshProfiles();
 // An omitted marker belongs to its page's old edge. Only when that edge's
 // messages are dropped at the kept limit does a marker above them go too.
 if(dropped)for(const n of [...list.children]){if(n.dataset?.id!==undefined)break;if(n.className==='conversation-omitted')n.remove();}
 settlePending();renderPending();activity();
 if(follow){following=true;list.scrollTop=list.scrollHeight;}
 else if(fresh.length)setStatus('New messages below · paused while you read');
 loadThumbnails();
}
/* An older page is oldest-first. At the kept limit keep its newest part, the
   part next to what is shown, and stop offering older pages so no gap opens. */
/* Marks a gap at the old edge of the rows just added. */
function gap(){const n=make('div','conversation-omitted',messages.omitted);n.setAttribute('role','note');return n;}
function prepend(page,omitted){
 const before=list.scrollHeight,first=[...list.children].find(x=>x!==olderButton)||null;
 let fresh=page.filter(e=>!seen.has(e.id));const room=Math.max(0,MAX_KEPT-events.length);
 if(fresh.length>room){fresh=room?fresh.slice(-room):[];older=null;setStatus(messages.kept_limit);}
 if(omitted)list.insertBefore(gap(),first);
 for(const e of fresh){seen.add(e.id);list.insertBefore(row(e),first);const shown=shownFiles(e);if(shown)list.insertBefore(shown,first);}
 events=fresh.concat(events);
 groupShellRows();
 regroupTools();
 refreshProfiles();
 list.scrollTop+=list.scrollHeight-before;following=false;savedTop=list.scrollTop;
 loadThumbnails();
}
function reset(){closePreview();forgetThumbnails();clearToolGroups();events=[];seen.clear();older=null;list.querySelectorAll(':scope > .conversation-shown-files').forEach(x=>x.remove());list.querySelectorAll(':scope > [data-id]').forEach(x=>x.remove());list.querySelectorAll(':scope > .conversation-omitted').forEach(x=>x.remove());}
function slow(on){if(retryButton)retryButton.hidden=!on;if(on)setStatus(messages.history_slow);}
function showError(v){
 const code=typeof v?.error==='string'?v.error:'history_unavailable';
 if(code==='history_slow'){slow(true);return;}
 const reason=typeof v?.reason_code==='string'&&/^[a-z0-9_]{1,64}$/.test(v.reason_code)?' ('+v.reason_code+')':'';
 setStatus((messages[code]||'History is unavailable right now.')+reason);
}
async function poll(){
 const g=gen;clearTimeout(timer);timer=0;
 if(!root||document.hidden)return;
 try{
  const v=await request(null,'poll');if(g!==gen)return;
  if(v.ok!==true){
   // A hidden, stale or changed record also hides any file opened from it;
   // a slow answer is only a delay and leaves it open.
   if(v.error!=='history_slow')closePreview();showError(v);if(v.error==='history_changed')reset();}
  else{
   failures=0;slow(false);if(!events.length&&v.omitted===true)list.insertBefore(gap(),list.querySelector(':scope > .conversation-pending'));append(v.events);
   // The latest page's older token stays valid only while the oldest shown
   // message is still in that page; it is renewed on every such poll.
   if(events.length&&v.events.some(e=>e.id===events[0].id))older=v.older;
   if(atBottom())setStatus(v.events.length?(v.tail_only?'Latest record · updating':'Read-only record · updating'):'No messages in the record yet.');
   if(v.limited&&!events.length)setStatus('The latest record was shortened.');
   olderButton.hidden=!older||loadingOlder;
  }
 }catch(e){if(g!==gen||e?.superseded)return;failures++;if(e?.slow)slow(true);else setStatus('History unavailable. Retrying.');}
 if(g===gen&&root&&!document.hidden)timer=setTimeout(poll,failures?Math.min(30000,POLL*2**failures):POLL);
}
async function loadOlder(){
 if(!older||loadingOlder)return;const g=gen,token=older;loadingOlder=true;olderButton.hidden=true;
 try{
  const v=await request(token,'older');if(g!==gen)return;
  if(v.ok!==true&&v.error==='page_expired'){
   // Start again from the latest page, which brings a fresh token.
   reset();loadingOlder=false;await poll();if(g===gen)setStatus(messages.page_expired);return;
  }
  // A slow answer keeps the token, so Show older can simply be tapped again.
  if(v.ok===true){older=v.older;prepend(v.events,v.omitted===true);}
  else if(v.error==='history_slow')setStatus(messages.older_slow);
  else{showError(v);older=null;}
 }catch(e){if(g===gen&&!e?.superseded)setStatus(e?.slow?messages.older_slow:'Older messages unavailable.');}
 finally{if(g===gen){loadingOlder=false;olderButton.hidden=!older;}}
}
function mount(n){
 unmount();name=n;root=make('div','conversation');root.id='conversation';
 status=make('p','conversation-status','Loading the record…');status.setAttribute('role','status');
 olderButton=make('button','conversation-older','Show older');olderButton.type='button';olderButton.hidden=true;olderButton.onclick=loadOlder;
 list=make('div','conversation-list');list.tabIndex=-1;list.setAttribute('aria-label','Conversation record for '+n);
 list.addEventListener('scroll',()=>{if(!list.isConnected)return;following=list.scrollHeight-list.clientHeight-list.scrollTop<=24;savedTop=list.scrollTop;if(following&&/paused/.test(status.textContent))setStatus('Read-only record · updating');},{passive:true});
 retryButton=make('button','conversation-retry','Retry');retryButton.type='button';retryButton.hidden=true;
 retryButton.onclick=()=>{retryButton.hidden=true;setStatus('Loading the record…');return poll();};
 list.append(olderButton);root.append(status,retryButton,list);
 if(typeof ResizeObserver==='function'){mailResize=new ResizeObserver(()=>{if(list&&list.clientWidth!==mailWidth){mailWidth=list.clientWidth;layoutMailRoutes();}});mailResize.observe(list);}
 const g=gen;document.fonts?.ready?.then(()=>{if(g===gen)layoutMailRoutes();});poll();return root;
}
function element(){return root;}
function addPending(text){if(!root||typeof text!=='string'||!text.trim())return;pending.push({text,at:Date.now(),before:new Set(seen)});renderPending();following=true;restore();}
function unmount(){
 gen++;clearTimeout(timer);timer=0;closePreview();forgetThumbnails();for(const k in controllers){controllers[k]?.abort();controllers[k]=null;}
 mailResize?.disconnect();mailResize=null;mailWidth=0;
 clearToolGroups();
 root?.replaceChildren();root?.remove();root=list=status=olderButton=retryButton=null;
 events=[];seen=new Set();older=null;pending=[];failures=0;loadingOlder=false;following=true;savedTop=0;name='';
}
document.addEventListener('visibilitychange',()=>{
 if(document.hidden){
  gen++;clearTimeout(timer);timer=0;
  if(controllers.preview&&previewRequest)previewRetry=true;
  for(const item of thumbnails.values())item.controller?.abort();
  for(const k in controllers){controllers[k]?.abort();controllers[k]=null;}
  loadingOlder=false;if(olderButton)olderButton.hidden=!older;
 }else if(root){
  if(previewRetry&&previewRequest&&panel){const x=previewRequest,focus=previewFocus;openPreview(x);previewFocus=focus;}
  restore();poll();
 }
});window.addEventListener('pagehide',unmount);
window.OrreryMobileConversation={enabled,mount,element,unmount,addPending,restore,dismissPreview,refreshActivity:activity,refreshProfiles};
})();
