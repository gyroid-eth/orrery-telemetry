/* Mobile viewport/selection adapter. Drawing, forces and tuning use PC core. */
(function(){
'use strict';
const core=window.OrreryNetworkCore;
class NetworkView{
 constructor(onSelect){
  this.svg=document.getElementById('gsvg');this.net=document.getElementById('net');
  this.map=new Map();this.elements={node:new Map(),edge:[],badge:[]};this.edges=[];this.spawn=[];
  this.params={...core.defaults};this.mode='focus';this.selected='';this.agents=[];
  this.view={vx:0,vy:0,k:1};this.raf=0;this.hot=0;this.userView=false;this.onSelect=onSelect;
  this.reduced=matchMedia('(prefers-reduced-motion:reduce)').matches;
  try{
   const saved=JSON.parse(localStorage.getItem('orrery.mobile.network')||'{}');
   for(const[k,p]of Object.entries(core.parameters))if(typeof saved[k]==='number'&&Number.isFinite(saved[k]))this.params[k]=Math.max(p.min,Math.min(p.max,saved[k]));
  }catch(_){}
  core.createDefs(this.svg);this.initControls();this.initPointers();
 }
 dimensions(){const r=this.svg.getBoundingClientRect();return {w:r.width||354,h:r.height||350};}
 world(){return this.mode==='all'?{w:1050,h:850}:{w:600,h:480};}
 update(snapshot){
  this.snapshot=snapshot;this.agents=snapshot.agents;
  if(!this.agents.some(a=>a.name===this.selected)){
   this.selected=this.agents.find(a=>a.running)?.name||this.agents[0]?.name||'';this.userView=false;
  }
  const picker=document.getElementById('agent-picker');
  const names=this.agents.map(a=>a.name);
  if(names.join('\0')!==this.namesKey){
   picker.replaceChildren();
   for(const n of names){const o=document.createElement('option');o.value=o.textContent=n;picker.appendChild(o);}
   if(!names.length){const o=document.createElement('option');o.value='';o.textContent='No agents yet';picker.appendChild(o);}
   this.namesKey=names.join('\0');
  }
  picker.value=this.selected;
  const key=JSON.stringify([this.mode,this.mode==='focus'?this.selected:'',
   snapshot.agents.map(a=>[a.name,a.provider,a.model,a.locality,a.running,a.retired,a.state,a.act_state,typeof a.ctx_used==='number'?Math.round(a.ctx_used):null]),
   snapshot.graph.edges.map(e=>[e.source,e.target,e.kind,e.count]),snapshot.graph.spawn]);
  if(key!==this.renderKey){this.renderKey=key;this.rebuild();}
  this.selection();
  const mailStatus=this.snapshot.sources.mail.status;
  const rosterSources=[this.snapshot.sources.agents,this.snapshot.sources.graph];
  const rosterObserved=rosterSources.some(s=>s.observed_at!==null&&s.status!=='unavailable');
  const rosterFresh=rosterSources.every(s=>s.status==='available');
  const prefix=mailStatus==='unavailable'?'Mail unavailable · ':!rosterFresh||mailStatus==='stale'?'Last known · ':'';
  document.getElementById('netinfo').textContent=!rosterObserved?'Agent roster unavailable':prefix+this.map.size+' / '+this.agents.length+' agents · '+this.edges.length+' Mail links'+(this.snapshot.timeline.edges_truncated?' · truncated':'');
 }
 rebuild(){
  if(!this.snapshot)return;
  let names=this.agents.map(a=>a.name);
  if(this.mode==='focus'){
   const peers=new Map();
   for(const e of this.snapshot.graph.edges){const n=e.source===this.selected?e.target:e.target===this.selected?e.source:null;if(n)peers.set(n,(peers.get(n)||0)+e.count);}
   for(const e of this.snapshot.graph.spawn){const n=e.source===this.selected?e.target:e.target===this.selected?e.source:null;if(n&&!peers.has(n))peers.set(n,0);}
   names=[this.selected,...[...peers].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])).slice(0,5).map(x=>x[0])].filter(Boolean);
  }
  const keep=new Set(names),old=this.map,world=this.world();this.map=new Map();
  names.forEach((n,i)=>{
   const a=this.agents.find(x=>x.name===n);if(!a)return;
   const prev=old.get(n),theta=i*2.3999632297,rad=35+Math.sqrt(i)*17;
   this.map.set(n,{x:prev?.x??world.w/2+Math.cos(theta)*rad,y:prev?.y??world.h/2+Math.sin(theta)*rad,vx:0,vy:0,fixed:false,
    provider:a.provider,model:a.model,locality:a.locality,running:a.running===true,retired:a.retired,
    state:a.state,present:a.state!=='gone'&&a.state!=='unknown',actState:a.act_state,ctxUsed:a.ctx_used,deg:0});
  });
  this.edges=this.snapshot.graph.edges.filter(e=>keep.has(e.source)&&keep.has(e.target));
  this.spawn=this.snapshot.graph.spawn.filter(e=>keep.has(e.source)&&keep.has(e.target));
  for(const e of this.edges){this.map.get(e.source).deg++;this.map.get(e.target).deg++;}
  this.net.classList.toggle('all-scope',this.mode==='all');this.net.classList.toggle('dense',this.map.size>300);
  this.drawElements();this.hot=150;this.run();
  if(!this.map.size)this.fit();
 }
 drawElements(){
  this.elements=core.buildElements(this.svg,this.map,this.edges,this.spawn,{
   widthMultiplier:this.params.LWMUL,
   render:(name,g)=>{
    const node=window.OrreryNetworkNode.render(name,g,{selected:name===this.selected});
    node.dataset.name=name;return node;
   },
   onEdgeClick:e=>{
    const a=e.currentTarget.dataset.s,b=e.currentTarget.dataset.t;
    if(e.currentTarget.dataset.kind==='spawn')return;
    const count=this.edges.filter(x=>(x.source===a&&x.target===b)||(x.source===b&&x.target===a)).reduce((sum,x)=>sum+x.count,0);
    document.getElementById('selection-label').textContent='Mail pair';
    document.getElementById('selected-name').textContent=a+' ↔ '+b;
    document.getElementById('context-detail').textContent=count+' recipient rows · '+this.snapshot.window;
   },events:{}
  });
  core.paint(this.map,this.elements,this.params.NSIZE);
 }
 run(){
  if(this.raf||document.hidden||document.getElementById('graph-panel').hidden)return;
  let settled=0,first=true;
  const frame=()=>{
   const cool=this.hot<30?.70:this.hot<60?.90:1;let move=0;
   for(let i=0;i<(this.reduced?6:1);i++)move=Math.max(move,core.step(this.map,this.edges,this.spawn,this.world(),this.params,cool));
   if(first||move>=.01)core.paint(this.map,this.elements,this.params.NSIZE);
   first=false;settled=move<.45?settled+1:0;this.hot--;
   if(!this.userView&&(this.hot%6===0||settled>=5))this.fit();
   if(this.hot>0&&settled<5)this.raf=requestAnimationFrame(frame);
   else{this.raf=0;if(!this.userView)this.fit();}
  };
  this.raf=requestAnimationFrame(frame);
 }
 pause(){if(this.raf)cancelAnimationFrame(this.raf);this.raf=0;}
 fit(){const v=core.fit(this.map,this.dimensions());if(v)this.view=v;this.applyView();}
 applyView(){
  const {w,h}=this.dimensions(),v=this.view;
  this.svg.setAttribute('viewBox',`${v.vx.toFixed(2)} ${v.vy.toFixed(2)} ${(w/v.k).toFixed(2)} ${(h/v.k).toFixed(2)}`);
  document.getElementById('zoom-label').textContent=Math.round(v.k*100)+'%';
  this.net.classList.toggle('overview',v.k<.75);
 }
 zoom(factor){
  const d=this.dimensions(),v=this.view,cx=v.vx+d.w/v.k/2,cy=v.vy+d.h/v.k/2;
  v.k=Math.max(.3,Math.min(4,v.k*factor));v.vx=cx-d.w/v.k/2;v.vy=cy-d.h/v.k/2;
  this.userView=true;this.applyView();
 }
 select(n,{center=false}={}){
  if(!this.agents.some(a=>a.name===n))return;
  this.selected=n;document.getElementById('agent-picker').value=n;
  if(this.mode==='focus'){this.userView=false;this.renderKey='';this.update(this.snapshot);}
  else{
   for(const[nm,o]of this.elements.node)o.grp.classList.toggle('selnode',nm===n);
   if(center){const g=this.map.get(n),d=this.dimensions();this.view={k:1.25,vx:g.x-d.w/2.5,vy:g.y-d.h/2.5};this.userView=true;this.applyView();}
  }
  this.selection();if(this.onSelect)this.onSelect(n);
 }
 selection(){
  const a=this.agents.find(x=>x.name===this.selected);
  document.getElementById('selection-label').textContent='Selected';
  document.getElementById('selected-name').textContent=a?.name||'None';
  const ctx=typeof a?.ctx_used==='number'?Math.round(100-a.ctx_used)+'% context left':'Context unknown';
  document.getElementById('context-detail').textContent=ctx+(a?' · '+(a.act_state||a.state):'');
 }
 initControls(){
  const rows=document.getElementById('nctl-rows');
  const bindings=Object.fromEntries(Object.keys(core.defaults).map(k=>[k,{get:()=>this.params[k],set:v=>this.params[k]=v}]));
  const tuning=core.tuning(bindings);
  const save=()=>{try{localStorage.setItem('orrery.mobile.network',JSON.stringify(this.params));}catch(_){}};
  this.syncControls=()=>{
   for(const row of rows.children){const k=row.dataset.p,p=core.parameters[k],v=this.params[k];row.querySelector('input').value=v;row.querySelector('.val').textContent=tuning[k].fmt(v);row.querySelector('input').style.setProperty('--fill',((v-p.min)/(p.max-p.min)*100)+'%');}
  };
  for(const[k,p]of Object.entries(core.parameters)){
   const row=document.createElement('div');row.className='st-row';row.dataset.p=k;
   const label=document.createElement('label');label.className='lab';label.htmlFor='net-'+k;label.textContent=p.label;
   const val=document.createElement('span');val.className='val';
   const input=document.createElement('input');input.type='range';input.id='net-'+k;input.min=p.min;input.max=p.max;input.step=p.step;input.setAttribute('aria-label',p.label);
   input.addEventListener('input',()=>{tuning[k].set(Number(input.value));this.syncControls();save();if(p.apply==='repaint')core.paint(this.map,this.elements,this.params.NSIZE);else{if(p.apply==='rebuild')this.drawElements();this.hot=Math.max(this.hot,80);this.run();}});
   row.append(label,val,input);rows.appendChild(row);
  }
  this.syncControls();
  document.getElementById('nc-reset').onclick=()=>{this.params={...core.defaults};this.syncControls();save();this.drawElements();this.hot=150;this.run();};
  document.getElementById('agent-picker').onchange=e=>this.select(e.target.value,{center:true});
  document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{
   if(!this.snapshot)return;
   this.mode=b.dataset.mode;this.map.clear();this.renderKey='';this.userView=false;
   document.querySelectorAll('[data-mode]').forEach(x=>x.classList.toggle('active',x===b));this.update(this.snapshot);
  });
  document.getElementById('zoom-out').onclick=()=>this.zoom(1/1.15);document.getElementById('zoom-in').onclick=()=>this.zoom(1.15);
  document.getElementById('fit').onclick=()=>{this.userView=false;this.fit();};
 }
 initPointers(){
  const points=new Map();let pan=null,pinch=null;
  const geometry=()=>{const[a,b]=[...points.values()];return{d:Math.hypot(a.x-b.x,a.y-b.y),x:(a.x+b.x)/2,y:(a.y+b.y)/2};};
  this.svg.addEventListener('pointerdown',e=>{
   points.set(e.pointerId,{x:e.clientX,y:e.clientY});this.svg.setPointerCapture(e.pointerId);
   if(points.size===2){const g=geometry(),r=this.svg.getBoundingClientRect();pinch={d:g.d,k:this.view.k,wx:this.view.vx+(g.x-r.x)/this.view.k,wy:this.view.vy+(g.y-r.y)/this.view.k};pan=null;return;}
   if(points.size===1)pan={id:e.pointerId,x:e.clientX,y:e.clientY,vx:this.view.vx,vy:this.view.vy,node:e.target.closest('.node')?.dataset.name,moved:false};
  });
  this.svg.addEventListener('pointermove',e=>{
   if(!points.has(e.pointerId))return;points.set(e.pointerId,{x:e.clientX,y:e.clientY});
   if(pinch&&points.size===2){const g=geometry(),r=this.svg.getBoundingClientRect(),k=Math.max(.3,Math.min(4,pinch.k*g.d/(pinch.d||1)));this.view={k,vx:pinch.wx-(g.x-r.x)/k,vy:pinch.wy-(g.y-r.y)/k};this.userView=true;this.applyView();return;}
   if(!pan||e.pointerId!==pan.id)return;
   if(Math.hypot(e.clientX-pan.x,e.clientY-pan.y)>5)pan.moved=true;
   if(pan.moved){this.view.vx=pan.vx-(e.clientX-pan.x)/this.view.k;this.view.vy=pan.vy-(e.clientY-pan.y)/this.view.k;this.userView=true;this.applyView();}
  });
  const end=e=>{
   points.delete(e.pointerId);if(pinch){if(points.size<2){pinch=null;pan=null;}return;}
   const old=pan;pan=null;if(old&&!old.moved&&old.node&&e.type==='pointerup')this.select(old.node);
  };
  this.svg.addEventListener('pointerup',end);this.svg.addEventListener('pointercancel',end);
  this.svg.addEventListener('wheel',e=>{e.preventDefault();this.zoom(e.deltaY<0?1.15:1/1.15);},{passive:false});
  window.addEventListener('resize',()=>this.applyView());
 }
}
window.OrreryMobileNetwork=NetworkView;
})();
