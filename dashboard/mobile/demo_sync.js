/* Follow the desktop's shared Demo display state without reloading drafts. */
(function(){
  'use strict';
  const html=document.documentElement;
  html.dataset.demoSync='pending';
  let running=null,last='',loaded=false;
  let wasOn=false;
  try{wasOn=localStorage.getItem('orrery.mobile.demo-active.v1')==='1';}catch(_){}
  function panel(error){
    html.dataset.demoSync='failed';
    let box=document.querySelector('.mobile-demo-status');
    if(!box){
      box=document.createElement('dialog');box.className='mobile-demo-status';box.setAttribute('role','alertdialog');box.setAttribute('aria-label','Checking Demo mode');
      box.innerHTML='<div><strong>Checking Demo mode</strong><p>The desktop display settings could not be loaded. Your screen stays hidden.</p><button type="button">Retry</button></div>';
      box.querySelector('button').onclick=refresh;
      box.addEventListener('cancel',event=>event.preventDefault());
      (document.body||html).append(box);
      box.showModal();
    }
    box.dataset.reason=error?.name||'unavailable';
  }
  function script(path){
    return new Promise((resolve,reject)=>{
      const element=document.createElement('script');element.src=path;
      const timer=setTimeout(()=>{element.remove();reject(new Error('Demo script timeout'));},5000);
      element.onload=()=>{clearTimeout(timer);resolve();};
      element.onerror=()=>{clearTimeout(timer);element.remove();reject(new Error('Demo script unavailable'));};
      document.head.append(element);
    });
  }
  async function apply(state){
    if(!state||state.version!==1||typeof state.supported!=='boolean'||typeof state.on!=='boolean'||!Array.isArray(state.words))throw new Error('Demo state unavailable');
    if(!state.supported&&wasOn)throw new Error('Waiting for the desktop Demo state');
    const key=JSON.stringify(state);
    if(key===last){html.dataset.demoSync='ready';document.querySelector('.mobile-demo-status')?.remove();return;}
    html.dataset.demoSync='pending';
    if(state.on&&!loaded){
      window.OrreryDemoBootstrap=state;
      if(!window.OrreryDemoMask)await script('/demo-mask.js');
      if(!window.OrreryDemo?.refresh)await script('/demo-mode.js');
      await Promise.race([window.OrreryDemo.ready,new Promise((_,reject)=>setTimeout(()=>reject(new Error('Demo mask timeout')),5500))]);
      loaded=true;
    }
    if(loaded)window.OrreryDemo.refresh(state);
    if(state.supported){wasOn=state.on;try{if(wasOn)localStorage.setItem('orrery.mobile.demo-active.v1','1');else localStorage.removeItem('orrery.mobile.demo-active.v1');}catch(_){}}
    last=key;html.dataset.demoSync='ready';document.querySelector('.mobile-demo-status')?.remove();
  }
  function refresh(){
    if(running)return running;
    const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),3500);
    running=fetch('/view/demo',{cache:'no-store',signal:abort.signal}).then(response=>{
      if(!response.ok)throw new Error('Demo state unavailable');return response.json();
    }).then(apply).catch(panel).finally(()=>{clearTimeout(timer);running=null;});
    return running;
  }
  window.OrreryMobileDemo=Object.freeze({refresh});
  refresh();setInterval(()=>{if(!document.hidden)refresh();},4000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
})();
