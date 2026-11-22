/* Shared page-session capability, fetch epoch and tab handshake. */
(function(){'use strict';
function create({onLock=()=>{},onRequired=()=>{},onAccept=async()=>{},onAccepted=()=>{}}={}){
 const nativeFetch=window.fetch.bind(window),storageKey='orrery.operator-capability';
 const memoryStorage=new Map(),requests=new Set(),urls=new Set();
 let memoryOnly=false,shareRequest=null,shareAllowed=false,capability='',epoch=0;
 function stored(scope,method,key,value){
  const k=scope+':'+key;
  if(!(scope==='sessionStorage'&&memoryOnly)){
   try{const store=window[scope];if(method==='getItem')return store.getItem(key);store[method](key,value);}
   catch(_){if(scope==='sessionStorage')memoryOnly=true;}
  }
  if(method==='getItem')return memoryStorage.get(k)||null;
  if(method==='setItem')memoryStorage.set(k,value);else memoryStorage.delete(k);
 }
 capability=stored('sessionStorage','getItem',storageKey)||'';
 const channel=typeof BroadcastChannel==='function'?new BroadcastChannel('orrery.operator-session'):null;
 function lock(broadcast=true){
  epoch++;shareRequest=null;shareAllowed=false;
  for(const request of requests)request.abort();requests.clear();
  for(const url of urls)URL.revokeObjectURL(url);urls.clear();
  capability='';stored('sessionStorage','removeItem',storageKey);onLock();
  if(broadcast)channel?.postMessage({type:'lock'});onRequired();
 }
 async function fetchValue(path,body,blob=false){
  const generation=epoch,token=capability;
  if(!token)throw Error('HTTP_OPERATOR_REQUIRED');
  // Consumers can choose a fixed same-origin path, never an alternate origin.
  if(typeof path!=='string'||!path.startsWith('/')||path.startsWith('//')||new URL(path,location.href).origin!==location.origin)throw Error('HTTP_OPERATOR_PATH_INVALID');
  const controller=new AbortController();requests.add(controller);
  try{
   const r=await nativeFetch(path,{signal:controller.signal,method:body?'POST':'GET',headers:{Authorization:'Bearer '+token,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined,cache:'no-store',redirect:'error'});
   if(generation!==epoch)throw Error('PAGE_LOCKED');
   const j=blob&&r.ok?await r.blob():await r.json();
   if(generation!==epoch)throw Error('PAGE_LOCKED');
   if(!r.ok||(!blob&&j.ok===false)){if(r.status===403)lock();throw Error(j.reason||'REQUEST_FAILED');}
   if(blob){const url=URL.createObjectURL(j);urls.add(url);return url;}return j;
  }finally{requests.delete(controller);}
 }
 async function accept(token){
  const generation=++epoch;shareRequest=null;capability=token;
  try{
   await onAccept();if(generation!==epoch)throw Error('PAGE_LOCKED');
   stored('sessionStorage','setItem',storageKey,capability);shareAllowed=true;onAccepted();
  }catch(error){if(generation===epoch){capability='';stored('sessionStorage','removeItem',storageKey);}throw error;}
 }
 channel?.addEventListener('message',async e=>{
  const m=e.data;
  if(m?.type==='lock'){lock(false);return;}
  if(m?.type==='request'&&capability&&shareAllowed&&typeof m.request_id==='string')channel.postMessage({type:'share',request_id:m.request_id,capability});
  if(m?.type==='share'&&!capability&&shareRequest&&m.request_id===shareRequest.id&&shareRequest.generation===epoch&&performance.now()<=shareRequest.deadline&&typeof m.capability==='string'){
   shareRequest=null;try{await accept(m.capability);}catch(_){onRequired();}
  }
 });
 async function restore(){
  if(capability){try{await accept(capability);return;}catch(error){lock();onRequired(error);return;}}
  onRequired();
  if(channel){shareRequest={id:crypto.randomUUID(),generation:epoch,deadline:performance.now()+500};channel.postMessage({type:'request',request_id:shareRequest.id});setTimeout(()=>{shareRequest=null;},500);}
 }
 return Object.freeze({api:(path,body)=>fetchValue(path,body),apiBlob:path=>fetchValue(path,null,true),stored,accept,restore,lock,get epoch(){return epoch;},get unlocked(){return !!capability;},get memoryOnly(){return memoryOnly;}});
}
window.OrreryOperatorSession=Object.freeze({version:1,create});
})();
