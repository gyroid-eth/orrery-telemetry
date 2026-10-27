/* Mail activity from successive metadata snapshots; no content requests. */
(function(root){
'use strict';
class Counts{
 constructor(){this.reset();}
 reset(){this.previous=null;this.scope=null;}
 observe(snapshot,active=true){
  const timeline=snapshot.timeline,mail=snapshot.sources.mail;
  if(!active||mail.status!=='available'||timeline.edges_truncated||!timeline.scope){this.reset();return [];}
  const scope=JSON.stringify([snapshot.window,timeline.scope]),counts=new Map();
  for(const edge of snapshot.graph.edges){
   if(!['to','cc'].includes(edge.kind)||!Number.isSafeInteger(edge.count)||edge.count<0)continue;
   const key=JSON.stringify([edge.source,edge.target]);
   const row=counts.get(key)||{source:edge.source,target:edge.target,count:0};
   row.count+=edge.count;counts.set(key,row);
  }
  const changes=[];
  if(this.previous&&this.scope===scope){
   for(const[key,row]of counts){
    const delta=row.count-(this.previous.get(key)?.count||0);
    if(delta>0&&row.source!==row.target)changes.push({source:row.source,target:row.target,delta});
   }
  }
  this.previous=counts;this.scope=scope;
  return changes;
 }
}
class MailComets{
 constructor(network){
  this.network=network;this.counts=new Counts();this.items=[];this.raf=0;
  this.layer=document.createElement('div');this.layer.className='mail-comets';network.net.appendChild(this.layer);
  this.motion=matchMedia('(prefers-reduced-motion:reduce)');
  this.motion.addEventListener('change',()=>this.clear());
 }
 update(snapshot){
  const active=!document.hidden&&!document.getElementById('graph-panel').hidden;
  const scope=this.counts.scope;
  const changes=this.counts.observe(snapshot,active);
  if(scope!==this.counts.scope)this.clear();
  if(!active){this.clear();return;}
  const now=performance.now();
  // Aggregate each directed pair, and bound simultaneous work on small screens.
  for(const change of changes.slice(0,12)){
   if(!this.network.map.has(change.source)||!this.network.map.has(change.target))continue;
   const button=document.createElement('button');button.type='button';button.className='mail-comet';
   button.setAttribute('aria-label',`Open Mail: ${change.source} to ${change.target}, +${change.delta}`);
   for(const cls of ['halo','tail','core']){const part=document.createElement('i');part.className=cls;button.appendChild(part);}
   const count=document.createElement('span');count.className='comet-count';count.textContent='+'+change.delta;button.appendChild(count);
   button.addEventListener('click',()=>{this.network.onMailPair?.(change.source,change.target);this.clear();});
   this.layer.appendChild(button);this.items.push({...change,button,start:now,reduced:this.motion.matches});
  }
  while(this.items.length>12)this.items.shift().button.remove();
  if(this.items.length&&!this.raf)this.raf=requestAnimationFrame(time=>this.frame(time));
 }
 frame(now){
  this.raf=0;
  if(document.hidden||document.getElementById('graph-panel').hidden){this.pause();return;}
  const {map,view}=this.network;
  this.items=this.items.filter(item=>{
   const a=map.get(item.source),b=map.get(item.target),age=now-item.start,duration=item.reduced?4500:3200;
   if(!a||!b||age>=duration){item.button.remove();return false;}
   const progress=item.reduced?.5:.12+.76*Math.min(1,age/2800);
   const x=(a.x+(b.x-a.x)*progress-view.vx)*view.k,y=(a.y+(b.y-a.y)*progress-view.vy)*view.k;
   item.button.style.left=x+'px';item.button.style.top=y+'px';
   item.button.style.setProperty('--angle',Math.atan2(b.y-a.y,b.x-a.x)+'rad');
   item.button.style.opacity=item.reduced?'1':String(Math.min(1,age/180,(duration-age)/400));
   item.button.classList.toggle('still',item.reduced);
   return true;
  });
  if(this.items.length)this.raf=requestAnimationFrame(time=>this.frame(time));
 }
 clear(){if(this.raf)cancelAnimationFrame(this.raf);this.raf=0;this.items=[];this.layer.replaceChildren();}
 pause(){this.clear();this.counts.reset();}
}
const api={Counts,MailComets};
if(typeof module==='object'&&module.exports)module.exports=api;
else root.OrreryMobileComets=api;
})(typeof window==='object'?window:globalThis);
