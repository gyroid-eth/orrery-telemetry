/* Mobile viewport/selection adapter. Drawing, forces and tuning use PC core. */
(function(){
'use strict';
const core=window.OrreryNetworkCore;
class NetworkView{
 constructor(onSelect,onMailPair,onOpen){
  this.svg=document.getElementById('gsvg');this.net=document.getElementById('net');
  this.map=new Map();this.elements={node:new Map(),edge:[],badge:[]};this.edges=[];this.spawn=[];
  this.params={...core.defaults};this.mode='focus';this.selected='';this.agents=[];
  this.view={vx:0,vy:0,k:1};this.raf=0;this.hot=0;this.userView=false;this.onSelect=onSelect;this.onMailPair=onMailPair;this.pointerEnded=-Infinity;
  this.reduced=matchMedia('(prefers-reduced-motion:reduce)').matches;
  this.onOpen=onOpen;
  try{
   const saved=JSON.parse(localStorage.getItem('orrery.mobile.network')||'{}');
   for(const[k,p]of Object.entries(core.parameters))if(typeof saved[k]==='number'&&Number.isFinite(saved[k]))this.params[k]=Math.max(p.min,Math.min(p.max,saved[k]));
  }catch(_){}
  core.createDefs(this.svg);this.initControls();this.initPointers();
  this.comets=new window.OrreryMobileComets.MailComets(this);
 }
 dimensions(){const r=this.svg.getBoundingClientRect();return {w:r.width||354,h:r.height||350};}
 world(){return this.mode==='all'?{w:1050,h:850}:{w:600,h:480};}
 update(snapshot){
  this.snapshot=snapshot;this.agents=snapshot.agents;
  if(!this.agents.some(a=>a.name===this.selected)){
   this.selected=this.agents.find(a=>a.running)?.name||this.agents[0]?.name||'';this.userView=false;
  }
  const names=this.agents.map(a=>a.name);
  const searchKey=JSON.stringify(this.agents.map(a=>[a.name,a.role]));
  if(searchKey!==this.searchKey){this.searchKey=searchKey;this.search();}
  const key=JSON.stringify([this.mode,this.mode==='focus'?this.selected:'',
   snapshot.agents.map(a=>[a.name,a.role,a.provider,a.model,a.locality,a.running,a.retired,a.state,a.act_state,typeof a.ctx_used==='number'?Math.round(a.ctx_used):null]),
   snapshot.graph.edges.map(e=>[e.source,e.target,e.kind,e.count]),snapshot.graph.spawn]);
  if(key!==this.renderKey){
   const layoutKey=JSON.stringify([this.mode,this.mode==='focus'?this.selected:'',names,
    snapshot.graph.edges.map(e=>[e.source,e.target,e.kind,e.count]),snapshot.graph.spawn]);
   this.renderKey=key;
   if(layoutKey!==this.layoutKey){this.layoutKey=layoutKey;this.rebuild();}
   else this.refreshMetadata();
  }
  this.comets.update(snapshot);
  this.selection();
  const mailStatus=this.snapshot.sources.mail.status;
  const rosterSources=[this.snapshot.sources.agents,this.snapshot.sources.graph];
  const rosterObserved=rosterSources.some(s=>s.observed_at!==null&&s.status!=='unavailable');
  const rosterFresh=rosterSources.every(s=>s.status==='available');
  const prefix=mailStatus==='unavailable'?'Mail unavailable · ':!rosterFresh||mailStatus==='stale'?'Last known · ':'';
  document.getElementById('netinfo').textContent=!rosterObserved?'Agent roster unavailable':prefix+this.map.size+' / '+this.agents.length+' agents · '+this.edges.length+' Mail links'+(this.snapshot.timeline.edges_truncated?' · truncated':'');
 }
 metadata(a){
  return {provider:a.provider,model:a.model,locality:a.locality,running:a.running===true,retired:a.retired,
   state:a.state,present:a.state!=='gone'&&a.state!=='unknown',actState:a.act_state,ctxUsed:a.ctx_used,roleLabel:a.role||''};
 }
 refreshMetadata(){
  const agents=new Map(this.agents.map(a=>[a.name,a]));let restack=false;
  for(const[name,g]of this.map){
   const meta=this.metadata(agents.get(name));
   if(!Object.keys(meta).some(key=>g[key]!==meta[key]))continue;
   restack=restack||['running','retired','state','actState'].some(key=>g[key]!==meta[key]);
   Object.assign(g,meta);
   const entry=this.elements.node.get(name),node=this.renderNode(name,g);
   const focused=entry.grp===document.activeElement;
   entry.grp.replaceWith(node);entry.grp=node;if(focused)node.focus({preventScroll:true});
  }
  if(restack){
   const running=[],attention=[];
   for(const[name,g]of this.map){
    const state=g.retired?'retired':(g.state||(g.running?'run':g.present?'finished':'gone'));
    if(state==='run'){running.push(this.elements.node.get(name).grp);if(g.actState==='ask'||g.actState==='question')attention.push(this.elements.node.get(name).grp);}
   }
   running.forEach(node=>this.svg.append(node));attention.forEach(node=>this.svg.append(node));
  }
  core.paint(this.map,this.elements,this.params.NSIZE);
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
    ...this.metadata(a),deg:0});
  });
  this.edges=this.snapshot.graph.edges.filter(e=>keep.has(e.source)&&keep.has(e.target));
  this.spawn=this.snapshot.graph.spawn.filter(e=>keep.has(e.source)&&keep.has(e.target));
  for(const e of this.edges){this.map.get(e.source).deg++;this.map.get(e.target).deg++;}
  this.net.classList.toggle('all-scope',this.mode==='all');this.net.classList.toggle('dense',this.map.size>300);
  this.drawElements();this.hot=150;this.run();
  if(!this.map.size)this.fit();
 }
 renderNode(name,g){
    const node=window.OrreryNetworkNode.render(name,{...g,annot:g.roleLabel?{role:g.roleLabel}:null},{selected:name===this.selected,avatar:n=>n});
    const hit=document.createElementNS('http://www.w3.org/2000/svg','circle');hit.setAttribute('class','mobile-node-hit');hit.setAttribute('r','24');node.prepend(hit);
    node.setAttribute('tabindex','0');node.setAttribute('role','button');
    node.setAttribute('aria-label',(this.mode==='focus'&&name!==this.selected?'Focus on ':'Open Reader for ')+name+(g.roleLabel?' · '+g.roleLabel:''));
    node.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();this.activateNode(name,{keyboard:true});}});
    // The mobile PNG endpoint already resolves custom/science fallback. Use
    // initials on decode failure instead of retrying the PC hi-resolution URL.
    const original=node.querySelector('.pimg');
    if(original){const img=original.cloneNode(true);original.replaceWith(img);img.addEventListener('error',()=>{
     img.remove();const text=document.createElementNS('http://www.w3.org/2000/svg','text');text.setAttribute('text-anchor','middle');text.setAttribute('dominant-baseline','central');text.setAttribute('class','portrait-initials');text.textContent=name.slice(0,2);node.appendChild(text);
    });}
    node.dataset.name=name;return node;
 }
 drawElements(){
  this.elements=core.buildElements(this.svg,this.map,this.edges,this.spawn,{
   widthMultiplier:this.params.LWMUL,
   render:(name,g)=>this.renderNode(name,g),
   onEdgeClick:e=>{
    // Pointer capture retargets touch clicks to the SVG. Pointerup below owns
    // those gestures; retain this listener only for non-pointer activation.
    if(performance.now()-this.pointerEnded<700)return;
    const a=e.currentTarget.dataset.s,b=e.currentTarget.dataset.t;
    if(e.currentTarget.dataset.kind==='spawn')return;
    this.onMailPair?.(a,b);
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
 pause(){if(this.raf)cancelAnimationFrame(this.raf);this.raf=0;this.comets.pause();}
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
 activateNode(name,{keyboard=false}={}){
  if(this.mode==='focus'&&name!==this.selected){
   this.select(name);this.fit();
   if(keyboard)this.elements.node.get(name)?.grp.focus({preventScroll:true});
   else if(this.svg.contains(document.activeElement))document.activeElement.blur();
  }else this.onOpen?.(name);
 }
 select(n,{center=false}={}){
  if(!this.agents.some(a=>a.name===n))return;
  this.selected=n;
  if(this.mode==='focus'){this.userView=false;this.renderKey='';this.update(this.snapshot);}
  else{
   for(const[nm,o]of this.elements.node)o.grp.classList.toggle('selnode',nm===n);
   if(center){const g=this.map.get(n),d=this.dimensions();this.view={k:1.25,vx:g.x-d.w/2.5,vy:g.y-d.h/2.5};this.userView=true;this.applyView();}
  }
  this.selection();if(this.onSelect)this.onSelect(n);
 }
 selection(){
  document.querySelector('[data-mode=focus]').title=this.selected?'Focus on '+this.selected:'Focus';
 }
 search(){
  const input=document.getElementById('agent-search'),results=document.getElementById('agent-results');
  const query=input.value.trim().toLocaleLowerCase();
  document.getElementById('search-clear').hidden=!query;
  results.hidden=!query;results.replaceChildren();if(!query)return;
  const matches=this.agents.filter(a=>(a.name+' '+(a.role||'')).toLocaleLowerCase().includes(query));
  for(const a of matches.slice(0,12)){
   const row=document.createElement('div');row.className='agent-result';
   const open=document.createElement('button');open.type='button';open.className='agent-result-open';
   const name=document.createElement('strong');name.textContent=a.name;
   const role=document.createElement('span');role.textContent=a.role||'Role not set';
   open.append(name,role);open.onclick=()=>this.onOpen?.(a.name);
   const focus=document.createElement('button');focus.type='button';focus.className='agent-result-focus';focus.textContent='Focus';focus.setAttribute('aria-label','Focus on '+a.name);
   focus.onclick=()=>{this.mode='focus';document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode==='focus'));this.select(a.name,{center:true});input.value='';this.search();document.querySelector('[data-mode=focus]').focus();};
   row.append(open,focus);results.append(row);
  }
  const note=document.createElement('p');note.className='agent-search-note';note.setAttribute('role','status');
  note.textContent=!matches.length?'No matching agents':matches.length>12?'Showing 12 of '+matches.length+' · keep typing to narrow your search':matches.length+' matching '+(matches.length===1?'agent':'agents');results.append(note);
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
  document.getElementById('agent-search').addEventListener('input',()=>this.search());
  document.getElementById('agent-search').addEventListener('keydown',event=>{if(event.key==='Escape'){event.target.value='';this.search();}else if(event.key==='ArrowDown'){event.preventDefault();document.querySelector('.agent-result-open')?.focus();}});
  document.getElementById('search-clear').onclick=()=>{const input=document.getElementById('agent-search');input.value='';this.search();input.focus();};
  document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{
   if(!this.snapshot)return;
   this.mode=b.dataset.mode;this.map.clear();this.layoutKey='';this.renderKey='';this.userView=false;
   document.querySelectorAll('[data-mode]').forEach(x=>x.classList.toggle('active',x===b));this.update(this.snapshot);
  });
  document.getElementById('zoom-out').onclick=()=>this.zoom(1/1.15);document.getElementById('zoom-in').onclick=()=>this.zoom(1.15);
  document.getElementById('fit').onclick=()=>{this.userView=false;this.fit();};
 }
 initPointers(){
  const points=new Map();let pan=null,pinch=null;
  const geometry=()=>{const[a,b]=[...points.values()];return{d:Math.hypot(a.x-b.x,a.y-b.y),x:(a.x+b.x)/2,y:(a.y+b.y)/2};};
  this.svg.addEventListener('pointerdown',e=>{
   const node=e.target.closest('.node')?.dataset.name;
   // A touch's compatibility mousedown can hit a different node after rebuild.
   if(this.mode==='focus'&&node&&node!==this.selected)e.preventDefault();
   points.set(e.pointerId,{x:e.clientX,y:e.clientY});this.svg.setPointerCapture(e.pointerId);
   if(points.size===2){const g=geometry(),r=this.svg.getBoundingClientRect();pinch={d:g.d,k:this.view.k,wx:this.view.vx+(g.x-r.x)/this.view.k,wy:this.view.vy+(g.y-r.y)/this.view.k};pan=null;return;}
   if(points.size===1){const hit=e.target.closest('.edge-hit');pan={id:e.pointerId,x:e.clientX,y:e.clientY,vx:this.view.vx,vy:this.view.vy,node:e.target.closest('.node')?.dataset.name,edge:hit?.dataset.kind==='comm'?[hit.dataset.s,hit.dataset.t]:null,moved:false};}
  });
  this.svg.addEventListener('pointermove',e=>{
   if(!points.has(e.pointerId))return;points.set(e.pointerId,{x:e.clientX,y:e.clientY});
   if(pinch&&points.size===2){const g=geometry(),r=this.svg.getBoundingClientRect(),k=Math.max(.3,Math.min(4,pinch.k*g.d/(pinch.d||1)));this.view={k,vx:pinch.wx-(g.x-r.x)/k,vy:pinch.wy-(g.y-r.y)/k};this.userView=true;this.applyView();return;}
   if(!pan||e.pointerId!==pan.id)return;
   if(Math.hypot(e.clientX-pan.x,e.clientY-pan.y)>5)pan.moved=true;
   if(pan.moved){this.view.vx=pan.vx-(e.clientX-pan.x)/this.view.k;this.view.vy=pan.vy-(e.clientY-pan.y)/this.view.k;this.userView=true;this.applyView();}
  });
  const end=e=>{
   this.pointerEnded=performance.now();points.delete(e.pointerId);if(pinch){if(points.size<2){pinch=null;pan=null;}return;}
   const old=pan;pan=null;
   if(old&&!old.moved&&e.type==='pointerup'){
    if(old.node)this.activateNode(old.node);
    else if(old.edge)this.onMailPair?.(...old.edge);
   }
  };
  this.svg.addEventListener('pointerup',end);this.svg.addEventListener('pointercancel',end);
  this.svg.addEventListener('wheel',e=>{e.preventDefault();this.zoom(e.deltaY<0?1.15:1/1.15);},{passive:false});
  window.addEventListener('resize',()=>this.applyView());
 }
}
window.OrreryMobileNetwork=NetworkView;
})();
