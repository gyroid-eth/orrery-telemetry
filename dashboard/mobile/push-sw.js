/* Notification delivery only: deliberately no fetch handler or offline cache. */
'use strict';
self.addEventListener('push',event=>{
 let value;try{value=event.data.json();}catch(_){return;}
 const clean=(s,n)=>typeof s==='string'&&Array.from(s).length<=n&&!/[\p{Cc}\p{Cs}]/u.test(s);
 if(!value||!clean(value.title,160)||!clean(value.body,600)||!/^[a-f0-9]{32}$/.test(value.tag)||!/^[A-Za-z][A-Za-z0-9_-]{0,95}$/.test(value.agent))return;
 event.waitUntil(self.registration.showNotification(value.title,{body:value.body,tag:value.tag,renotify:false,icon:'/icons/orbit-192.png',badge:'/icons/orbit-192.png',data:{agent:value.agent}}));
});
self.addEventListener('notificationclick',event=>{
 event.notification.close();const agent=event.notification.data?.agent;
 if(typeof agent!=='string'||!/^[A-Za-z][A-Za-z0-9_-]{0,95}$/.test(agent))return;
 const destination=new URL('/',self.location.origin);destination.hash='notify='+agent;
 event.waitUntil((async()=>{
  const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
  const existing=windows.find(c=>{const u=new URL(c.url);return u.origin===destination.origin&&u.pathname==='/';});
  if(existing){await existing.navigate(destination.href);await existing.focus();}
  else await self.clients.openWindow(destination.href);
 })());
});
