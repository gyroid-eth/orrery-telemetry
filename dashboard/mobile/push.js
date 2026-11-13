/* Reader-scoped controls; permission starts only in the Enable click handler.
   Endpoints and keys remain in the browser's native PushSubscription, never
   URLs, application localStorage, logs, or the conversation draft. */
(()=>{'use strict';
const make=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;};
const defaults=()=>({enabled:false,reply_needed:true,finished:false,previews:false});
const states=new Map();let panel=null,agent='',current=defaults(),sub=null,available=false,busy=false,message='';
const supported=()=>window.isSecureContext&&'Notification' in window&&'serviceWorker' in navigator&&'PushManager' in window;
const demo=()=>document.documentElement.dataset.demo==='on';
async function request(operation,value){
 const c=new AbortController(),timer=setTimeout(()=>c.abort(),8000);
 try{
  const r=await fetch('/view/push'+(operation?'/'+operation:''),{method:operation?'POST':'GET',credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',signal:c.signal,...(operation?{headers:{'Content-Type':'application/json'},body:JSON.stringify(value)}:{})});
  const v=await r.json();if(!r.ok)throw Error(v.error||'push_unavailable');return v;
 }finally{clearTimeout(timer);}
}
async function localSubscription(){const registration=await navigator.serviceWorker.getRegistration('/');return registration?registration.pushManager.getSubscription():null;}
async function state(name){
 const cap=await request('');if(!cap.available||!supported())return {available:false,state:defaults(),sub:null};
 const subscription=await localSubscription();const value=subscription?await request('status',{subscription:subscription.toJSON(),agent:name}):defaults();
 return {available:true,state:value,sub:subscription};
}
function updateBells(name,value){
 states.set(name,value);document.querySelectorAll('.reader-push-bell').forEach(b=>{
  if(b.dataset.agent!==name)return;b.classList.toggle('on',value.enabled&&value.finished);b.setAttribute('aria-pressed',String(value.enabled&&value.finished));
 });
}
function close(){if(panel){panel.close();panel.remove();panel=null;}agent='';}
const explain=code=>({device_limit:'This Mac already has eight phones. Remove an unused phone before adding this one.',agent_limit:'Too many agents selected. Turn off an unused agent first.',agent_unavailable:'This agent is no longer available.',subscription_invalid:'This browser subscription is out of date. Disable it on this phone and enable again.'}[code]||'Could not update notifications. Check the connection and try again.');
function draw(){
 if(!panel)return;panel.replaceChildren();
 const handle=make('div','push-handle'),head=make('div','push-head'),title=make('h2','','Agent notifications'),done=make('button','','Done');done.type='button';done.onclick=close;head.append(title,done);panel.append(handle,head,make('p','push-agent',agent));
 const note=make('p','push-message',message);note.setAttribute('role','status');panel.append(note);
 if(!current.enabled){
  const enable=make('button','push-enable','Enable on this phone');enable.type='button';enable.disabled=busy||!available||demo();enable.onclick=enablePhone;panel.append(enable);
 }else panel.append(make('p','push-enabled','✓ Enabled on this phone'));
 for(const [key,label,description]of[['reply_needed','Reply needed','All agents · Questions and approvals'],['finished','Work finished','This agent · End of a turn'],['previews','Message previews','Show a short part of questions and replies']]){
  const row=make('label','push-row'),copy=make('span','push-copy'),input=make('input');input.type='checkbox';input.checked=current[key];input.disabled=busy||!current.enabled||demo();input.dataset.setting=key;
  copy.append(make('strong','',label),make('small','',description));row.append(copy,input,make('span','push-switch'));input.onchange=()=>change(key,input.checked);panel.append(row);
 }
 panel.append(make('p','push-footnote','Previews are off by default. Notifications pause while this Mac sleeps.'));
 if(sub){const off=make('button','push-disable','Disable on this phone');off.type='button';off.disabled=busy;off.onclick=disablePhone;panel.append(off);}
}
async function open(name){
 close();agent=name;current=states.get(name)||defaults();sub=null;available=false;busy=true;message='Checking this phone…';
 panel=make('dialog','push-sheet');panel.setAttribute('aria-label','Agent notifications');document.body.append(panel);panel.addEventListener('cancel',e=>{e.preventDefault();close();});draw();panel.showModal();const own=panel;
 try{
  if(!supported()){message='Use Android Chrome over HTTPS to enable notifications.';return;}
  const v=await state(name);if(panel!==own)return;available=v.available;current=v.state;sub=v.sub;updateBells(name,current);
  message=demo()?'Notifications are paused in Demo.':!available?'Phone notifications have not been set up on this Mac.':Notification.permission==='denied'?'Notifications are blocked. Allow ORRERY in Chrome’s site settings.':'';
 }catch(_){message='Could not check notifications. Check the connection and try again.';}
 finally{if(panel===own){busy=false;draw();}}
}
async function enablePhone(){
 if(busy||!available||demo())return;const own=panel,name=agent,settings={...current};
 // Invoke directly in this click, before the first await.
 const permission=Notification.requestPermission();busy=true;message='Waiting for notification permission…';draw();
 try{
  if(await permission!=='granted'){message='Notifications were not allowed. You can enable them in Chrome’s site settings.';return;}
  if(panel!==own)return;
  const key=await request('key',{});const registration=await navigator.serviceWorker.register('/push-sw.js',{scope:'/'});
  await navigator.serviceWorker.ready;
  const raw=atob(key.key.replace(/-/g,'+').replace(/_/g,'/'));const applicationServerKey=Uint8Array.from(raw,c=>c.charCodeAt(0));
  const selected=await registration.pushManager.getSubscription()||await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey});
  if(panel!==own)return;
  sub=selected;
  const result=await request('save',{subscription:selected.toJSON(),agent:name,reply_needed:settings.reply_needed,finished:settings.finished,previews:settings.previews});
  if(panel!==own)return;current=result;updateBells(name,current);message='Enabled on this phone.';
 }catch(e){message=explain(e.message);}
 finally{if(panel===own){busy=false;draw();}}
}
async function change(key,value){
 if(busy||!sub)return;const own=panel,name=agent,change={...current,[key]:value};busy=true;message='Saving…';draw();
 try{const result=await request('save',{subscription:sub.toJSON(),agent:name,reply_needed:change.reply_needed,finished:change.finished,previews:change.previews});if(panel!==own)return;current=result;updateBells(name,current);message='Saved for this phone.';}
 catch(e){message=explain(e.message);}finally{if(panel===own){busy=false;draw();}}
}
async function disablePhone(){
 if(busy||!sub)return;const own=panel,name=agent,old=sub;busy=true;message='Disabling…';draw();
 try{await request('remove',{subscription:old.toJSON()});await old.unsubscribe();if(panel!==own)return;sub=null;current=defaults();states.clear();document.querySelectorAll('.reader-push-bell').forEach(b=>{b.classList.remove('on');b.setAttribute('aria-pressed','false');});message='Disabled on this phone.';}
 catch(e){message=explain(e.message);}finally{if(panel===own){busy=false;draw();}}
}
function button(name){
 const b=make('button','reader-icon reader-push-bell');b.type='button';b.dataset.agent=name;b.setAttribute('aria-label','Notifications for '+name);b.setAttribute('aria-pressed',String(!!states.get(name)?.finished));b.title='Agent notifications';
 const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'),path=document.createElementNS('http://www.w3.org/2000/svg','path');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');path.setAttribute('d','M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4');svg.append(path);b.append(svg);b.onclick=()=>{if(!b.disabled)open(name);};
 if(states.get(name)?.enabled&&states.get(name)?.finished)b.classList.add('on');
 if(supported()&&!states.has(name))state(name).then(v=>updateBells(name,v.state)).catch(()=>{});return b;
}
window.OrreryMobilePush={button,close};
})();
