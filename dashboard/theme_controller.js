(function(){
  'use strict';
  const core=window.AgentStackColorThemeCore;
  if(!core)return;
  const root=document.documentElement;
  const embedded=window.parent!==window&&new URLSearchParams(location.search).get('embed')==='1';
  // A standalone page keeps its own preference in this browser. An embedded
  // page is told by the cockpit and never reads storage.
  const standalone=!embedded;
  const STORAGE_KEY='agentdash.colorTheme';
  const preferences=new Set(['dark','light','system']);
  const modes=new Set(['dark','light']);
  const theme=core.deriveWarmPaperLightTheme();
  const properties=Object.keys(theme.cssVariables);
  let state=Object.freeze({preference:'dark',resolved:'dark'});

  function normalize(data){
    if(!data||data.type!=='orrery-color-theme'||data.version!==1||
        !preferences.has(data.preference)||!modes.has(data.resolved))return null;
    if(data.preference!=='system'&&data.preference!==data.resolved)return null;
    return Object.freeze({preference:data.preference,resolved:data.resolved});
  }
  function allowed(event){
    return Boolean(embedded&&event.source===window.parent&&event.origin===location.origin);
  }
  function clear(){
    properties.forEach(property=>root.style.removeProperty(property));
    root.removeAttribute('data-color-theme');
    root.removeAttribute('data-color-theme-preference');
  }
  function apply(next){
    clear();state=next;
    if(next.resolved==='light'){
      Object.entries(theme.cssVariables).forEach(([property,value])=>root.style.setProperty(property,value));
      root.dataset.colorTheme='light';
    }
    root.dataset.colorThemePreference=next.preference;
    notify();
    return Object.freeze({ok:true,...next,themeId:next.resolved==='light'?theme.id:'dark'});
  }
  function notify(){
    try{
      if(typeof window.dispatchEvent==='function'&&typeof CustomEvent==='function'){
        window.dispatchEvent(new CustomEvent('orrery-color-theme-change',{detail:state}));
      }
    }catch(e){}
  }
  function systemResolved(){
    try{
      return window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';
    }catch(e){return 'dark';}
  }
  function resolve(preference){
    return preference==='system'?systemResolved():preference;
  }
  function readStored(){
    try{
      const value=localStorage.getItem(STORAGE_KEY);
      return preferences.has(value)?value:null;
    }catch(e){return null;}
  }
  function setPreference(preference){
    if(!standalone)return Object.freeze({ok:false,reason:'embedded'});
    if(!preferences.has(preference))return Object.freeze({ok:false,reason:'invalid-preference'});
    try{localStorage.setItem(STORAGE_KEY,preference);}catch(e){}
    return apply(Object.freeze({preference,resolved:resolve(preference)}));
  }
  function reply(result){
    if(!embedded)return;
    window.parent.postMessage({type:'orrery-color-theme-result',version:1,...result},location.origin);
  }
  window.addEventListener('message',event=>{
    if(!allowed(event))return;
    const next=normalize(event.data);
    if(!next){
      if(event.data?.type==='orrery-color-theme')reply({ok:false,reason:'invalid-message'});
      return;
    }
    reply(apply(next));
  });
  window.AgentStackColorTheme=Object.freeze({
    get state(){return state;},get lightTheme(){return theme;},
    get standalone(){return standalone;},normalize,apply,setPreference,
  });
  if(standalone){
    const stored=readStored();
    if(stored)apply(Object.freeze({preference:stored,resolved:resolve(stored)}));
    try{
      const query=window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)');
      if(query&&typeof query.addEventListener==='function'){
        query.addEventListener('change',()=>{
          if(state.preference==='system')apply(Object.freeze({preference:'system',resolved:resolve('system')}));
        });
      }
    }catch(e){}
  }
  const ready=()=>{
    if(embedded)window.parent.postMessage({type:'orrery-color-theme-ready',version:1},location.origin);
  };
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',ready,{once:true});
  else ready();
})();
