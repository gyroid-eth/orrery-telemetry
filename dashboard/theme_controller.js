(function(){
  'use strict';
  const core=window.AgentStackColorThemeCore;
  if(!core)return;
  const root=document.documentElement;
  const embedded=window.parent!==window&&new URLSearchParams(location.search).get('embed')==='1';
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
    return Object.freeze({ok:true,...next,themeId:next.resolved==='light'?theme.id:'dark'});
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
    get state(){return state;},get lightTheme(){return theme;},normalize,apply,
  });
  const ready=()=>{
    if(embedded)window.parent.postMessage({type:'orrery-color-theme-ready',version:1},location.origin);
  };
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',ready,{once:true});
  else ready();
})();
