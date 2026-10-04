/* Help map: the telemetry's controls annotated in place.
 *
 * Gold corner ticks frame each control and a hairline leader carries its note
 * into free space beside it; when a note finds no free place, every note moves
 * into a legend instead. The visual grammar is the cockpit's help map
 * (gyroid-eth/orrery bridge/cockpit_tour.js); the cockpit lays its notes out
 * in two columns around an empty stage, while this page has no empty stage,
 * so each note searches the free space around its own control.
 *
 * The notes cover what is on screen now: the deck, the network, or the agent
 * panel when it is open. A note whose control is not showing is left out.
 */
(function(root){
'use strict';
const NOTES=Object.freeze([
  {id:'view',label:'Deck and Network',target:['.viewtog'],copy:'Switch between the agent cards and the network of who talks to whom.'},
  {id:'crew',label:'Working and waiting',target:['header .crew-row'],copy:'How many of your running agents are working right now, and how many are waiting for their next instruction.'},
  // Same words as the cockpit's help map, which annotates the same LEFT.
  {id:'usage',label:'Usage left',target:['#usage-pill'],copy:'LEFT shows how much account allowance remains for Claude and Codex. Open it to see each window and when it resets.'},
  {id:'filter',label:'Filter',target:['#q'],copy:'Find agents by name or task.'},
  {id:'history',label:'Live, 7D, 30D, All',target:['#history'],copy:'Live shows running and finished sessions; 7D, 30D and All add agents that are gone or retired, from the last week, month, or ever.'},
  {id:'new',label:'New agent',target:['#newbtn'],copy:'Start an agent with a task and a model.'},
  {id:'card',label:'Agent card',target:['.bay .top'],copy:'One agent: its model, context left, task and state. Click the card for the details.'},
  {id:'exit',label:'Exit',target:['.bay .exitbtn'],copy:'End the agent gracefully. An agent whose session can be resumed comes back later with Resume.'},
  {id:'window',label:'Time window',target:['#winsel'],copy:'How far back the network reaches. ALL shows everything.'},
  {id:'select',label:'Select',target:['#selToggle'],copy:'Pick several agents, then exit, resume or replay them from the bar below.'},
  {id:'settings',label:'Settings',target:['#settings-btn'],copy:'The theme, and how the network is drawn.'},
  {id:'agent',label:'Agent',target:['#net g.node .node-halo'],copy:'An agent, with its role. Click it for the details.'},
  {id:'link',label:'Link',target:['#net .edge-count'],copy:'Mail between two agents, and how many. Click the line to read them.'},
  {id:'replay',label:'Replay',target:['#selbarReplay'],copy:'Watch the selected agents work together again.'},
  {id:'resume',label:'Resume',target:['#selbarResume'],copy:'Bring back the selected agents that have ended, where their session can be resumed.'},
  {id:'tabs',panel:true,label:'History and Output',target:['#tm-tabs'],copy:'What the agent did, step by step, and the files it produced.'},
  {id:'actions',panel:true,label:'Exit, Open, Close',target:['#tm-exit-btn','#tm-open','#tm-x'],copy:'End the agent, open its terminal (or resume it, when an ended session can be resumed), or close this panel.'},
  {id:'role',panel:true,label:'Role',target:['#tm-annot'],copy:'Give the agent a role label, shown with it in the network.'},
]);
const API={NOTES};
if(typeof module!=='undefined'&&module.exports)module.exports=API;
root.TelemetryHelpMap=API;
const SVG='http://www.w3.org/2000/svg';

const doc=root.document;
function visibleRect(el){
  if(!el)return null;
  const r=el.getBoundingClientRect(),cs=root.getComputedStyle(el);
  if(r.width<2||r.height<2||cs.visibility==='hidden'||cs.display==='none'||+cs.opacity===0)return null;
  const l=Math.max(0,r.left),t=Math.max(0,r.top),rr=Math.min(root.innerWidth,r.right),b=Math.min(root.innerHeight,r.bottom);
  return rr-l>2&&b-t>2?{l,t,r:rr,b}:null;
}
// The first showing match of a selector, or the span of several controls.
function targetRect(note){
  if(note.target.length>1&&note.id==='actions'){
    const rects=note.target.map(sel=>visibleRect(doc.querySelector(sel))).filter(Boolean);
    if(!rects.length)return null;
    return {l:Math.min(...rects.map(r=>r.l)),t:Math.min(...rects.map(r=>r.t)),r:Math.max(...rects.map(r=>r.r)),b:Math.max(...rects.map(r=>r.b))};
  }
  for(const sel of note.target)for(const el of doc.querySelectorAll(sel)){const r=visibleRect(el);if(r)return r;}
  return null;
}
function panelOpen(){const term=doc.getElementById('term');return !!(term&&term.classList.contains('on'));}

/* Geometry for the placement search. Leaders are runs of horizontal and
   vertical segments [x1,y1,x2,y2]. */
const overlaps=(a,b,pad=0)=>a.l<b.r+pad&&a.r+pad>b.l&&a.t<b.b+pad&&a.b+pad>b.t;
function segmentHitsRect(s,r){
  const l=Math.min(s[0],s[2]),rr=Math.max(s[0],s[2]),t=Math.min(s[1],s[3]),b=Math.max(s[1],s[3]);
  return l<r.r&&rr>r.l&&t<r.b&&b>r.t;
}
function segmentsMeet(a,b){
  const ah=a[1]===a[3],bh=b[1]===b[3];
  const inside=(v,p,q)=>v>Math.min(p,q)-1&&v<Math.max(p,q)+1;
  if(ah!==bh){const h=ah?a:b,v=ah?b:a;return inside(v[0],h[0],h[2])&&inside(h[1],v[1],v[3]);}
  if(ah)return Math.abs(a[1]-b[1])<4&&Math.min(Math.max(a[0],a[2]),Math.max(b[0],b[2]))>Math.max(Math.min(a[0],a[2]),Math.min(b[0],b[2]));
  return Math.abs(a[0]-b[0])<4&&Math.min(Math.max(a[1],a[3]),Math.max(b[1],b[3]))>Math.max(Math.min(a[1],a[3]),Math.min(b[1],b[3]));
}
function pathOf(points){return 'M'+points.map(p=>p.join(',')).join('L');}
function segmentsOf(points){const out=[];for(let i=1;i<points.length;i++)out.push([...points[i-1],...points[i]]);return out;}

/* Where a note may go: free spots on a grid over the screen, nearest first.
   The leader leaves its control from the side facing the note, at any point
   along that side, and bends once to reach the note's dot beside its name.
   A note right of its dot reads left-aligned; one left of it, right-aligned. */
function spread(lo,hi,step,toward){
  const out=[];for(let v=lo;v<=hi;v+=step)out.push(Math.round(v));
  if(!out.length)out.push(Math.round((lo+hi)/2));
  return out.sort((a,b)=>Math.abs(a-toward)-Math.abs(b-toward));
}
function* candidates(r,w,h,mid,W,H){
  const cx=(r.l+r.r)/2,cy=(r.t+r.b)/2,dots=[];
  for(let y=16;y<H-16;y+=22)for(let x=16;x<W-16;x+=30)dots.push([x,y,Math.hypot(x-cx,y-cy)]);
  dots.sort((a,b)=>a[2]-b[2]);
  for(const [x,y] of dots)for(const end of [false,true]){
    const box={x:end?x-10-w:x+10,y:y-mid,end,dot:[x,y]};
    // Arriving along the name line, the leader must come from the side away from the words.
    if(y>r.b+16||y<r.t-16){
      const edge=y>r.b?r.b+3:r.t-3;
      for(const ax of spread(r.l+6,r.r-6,8,x).slice(0,4)){
        if(end?ax<x:ax>x)continue;
        yield {...box,points:ax===x?[[ax,edge],[x,y]]:[[ax,edge],[ax,y],[x,y]]};
      }
      // Hemmed in below (or above): step into the gutter beside the control,
      // run along it, then turn to the note.
      const dir=y>r.b?1:-1;
      for(const g of [8,14])for(const ax of spread(r.l+6,r.r-6,8,x).slice(0,2)){
        const gy=edge+dir*g;
        yield {...box,points:[[ax,edge],[ax,gy],[x,gy],[x,y]]};
      }
    }
    if(x>r.r+16||x<r.l-16){
      const edge=x>r.r?r.r+3:r.l-3;
      for(const ay of spread(r.t+4,r.b-4,8,y).slice(0,3)){
        yield {...box,points:ay===y?[[edge,ay],[x,y]]:[[edge,ay],[x,ay],[x,y]]};
      }
    }
  }
}

API.geometry={overlaps,segmentHitsRect,segmentsMeet,candidates};
API.targetRect=note=>targetRect(note);
if(!doc)return;

function mount(){
  const header=doc.querySelector('body>header');
  const settingsBtn=doc.getElementById('settings-btn');
  if(!header)return;
  const button=doc.createElement('button');
  button.type='button';button.className='helpmap-btn';button.id='helpmap-btn';
  button.setAttribute('aria-pressed','false');button.title='Help map: what each control on this screen does';
  button.textContent='Help map';
  if(settingsBtn)settingsBtn.before(button);else header.append(button);
  // The agent panel covers the header (its backdrop sits above it), so the
  // panel carries its own way in; both open the same map, which then
  // annotates the panel.
  const panelButton=doc.createElement('button');
  panelButton.type='button';panelButton.className='tm-open tm-helpmap';panelButton.id='tm-helpmap';
  panelButton.setAttribute('aria-pressed','false');panelButton.title='Help map: what each control in this panel does';
  panelButton.textContent='HELP MAP';
  const panelExit=doc.getElementById('tm-exit-btn');
  if(panelExit)panelExit.before(panelButton);
  const buttons=[button,panelButton];

  const map=doc.createElement('section');
  map.className='help-map';map.hidden=true;map.setAttribute('aria-label','Telemetry help map');
  map.innerHTML='<svg class="help-map-art" aria-hidden="true"><defs><mask id="helpMapMask" maskUnits="userSpaceOnUse"></mask></defs>'+
    '<rect class="help-map-veil" mask="url(#helpMapMask)"/><g class="help-map-marks"></g></svg>'+
    '<div class="help-map-title"><b>Telemetry, annotated</b><span>Press Esc or click anywhere to close.</span>'+
    '<button type="button" class="help-map-close">Close</button></div><div class="help-map-notes"></div>';
  doc.body.append(map);
  const art=map.querySelector('svg'),mask=art.querySelector('mask'),marks=art.querySelector('.help-map-marks');
  const veil=art.querySelector('.help-map-veil'),notesEl=map.querySelector('.help-map-notes'),title=map.querySelector('.help-map-title');
  const noteEls=new Map(NOTES.map(note=>{
    const el=doc.createElement('article');el.className='help-map-note';el.dataset.note=note.id;
    const h=doc.createElement('h3');h.textContent=note.label;
    const p=doc.createElement('p');p.textContent=note.copy;el.append(h,p);notesEl.append(el);
    return [note.id,el];
  }));
  let open=false,previousFocus=null,lastKey='';
  function svg(tag,attrs,parent){const el=doc.createElementNS(SVG,tag);for(const k in attrs)el.setAttribute(k,attrs[k]);parent.append(el);return el;}

  function shown(){
    const inPanel=panelOpen();
    return NOTES.filter(note=>!!note.panel===inPanel).map(note=>({note,el:noteEls.get(note.id),r:targetRect(note)})).filter(item=>item.r);
  }
  function drawFrames(items){
    mask.replaceChildren();marks.replaceChildren();
    svg('rect',{width:'100%',height:'100%',fill:'white'},mask);
    items.forEach(({r})=>{
      const p=3,x=Math.max(1,r.l-p),y=Math.max(1,r.t-p);
      const w=Math.min(root.innerWidth-1,r.r+p)-x,h=Math.min(root.innerHeight-1,r.b+p)-y,a=Math.min(10,w/3,h/3);
      svg('rect',{x,y,width:w,height:h,rx:5,fill:'black'},mask);
      svg('path',{class:'help-map-frame',d:[[x,y,1,1],[x+w,y,-1,1],[x,y+h,1,-1],[x+w,y+h,-1,-1]].map(([cx,cy,dx,dy])=>`M${cx+dx*a},${cy}H${cx}V${cy+dy*a}`).join('')},marks);
    });
  }
  // Every note in a free place, or false.
  function annotate(items){
    const W=root.innerWidth,H=root.innerHeight,margin=10;
    if(W<760||H<480)return false;
    const width=Math.min(250,Math.max(190,W*.17));
    const blocked=items.map(item=>({l:item.r.l-6,t:item.r.t-6,r:item.r.r+6,b:item.r.b+6}));
    // Greedy, top to bottom; a note that finds no place goes first next time,
    // since an earlier note's leader may have taken the only way out.
    let order=items.slice().sort((a,b)=>(a.r.t-b.r.t)||(a.r.l-b.r.l));
    let placedNotes,placedSegments,stuck=null;
    for(let attempt=0;attempt<=items.length*3;attempt++){
      stuck=placeAll(order);
      if(!stuck)break;
      order=[stuck,...order.filter(item=>item!==stuck)];
    }
    if(stuck){map.dataset.fallback='no place for '+stuck.note.id;return false;}
    function placeAll(order){
    placedNotes=[];placedSegments=[];
    items.forEach(item=>{delete item.place;});
    for(const item of order){
      const el=item.el;el.classList.remove('end');el.style.width=width+'px';el.style.left='0';el.style.top='0';
      const h=el.offsetHeight,head=el.firstElementChild,mid=head.offsetTop+head.offsetHeight/2;
      let chosen=null;
      for(const c of candidates(item.r,width,h,mid,W,H)){
        const box={l:c.x,t:c.y,r:c.x+width,b:c.y+h};
        if(box.l<margin||box.t<margin||box.r>W-margin||box.b>H-margin)continue;
        if(blocked.some(b=>overlaps(box,b)))continue;
        if(placedNotes.some(n=>overlaps(box,n,14)))continue;
        const segs=segmentsOf(c.points);
        // A leader may pass close by other controls but not through them (its own
        // container, like the card around an Exit button, excepted).
        const others=items.filter(o=>o!==item&&!(o.r.l<=item.r.l&&o.r.r>=item.r.r&&o.r.t<=item.r.t&&o.r.b>=item.r.b))
          .map(o=>({l:o.r.l-2,t:o.r.t-2,r:o.r.r+2,b:o.r.b+2}));
        if(segs.some(s=>others.some(b=>segmentHitsRect(s,b))))continue;
        if(segs.some(s=>placedNotes.some(n=>segmentHitsRect(s,{l:n.l-4,t:n.t-4,r:n.r+4,b:n.b+4}))))continue;
        if(segs.some(s=>placedSegments.some(o=>segmentsMeet(s,o))))continue;
        if(placedSegments.some(o=>segmentHitsRect(o,{l:box.l-4,t:box.t-4,r:box.r+4,b:box.b+4})))continue;
        chosen={...c,box,segs};break;
      }
      if(!chosen)return item;
      item.place=chosen;placedNotes.push(chosen.box);placedSegments.push(...chosen.segs);
    }
    return null;
    }
    // The title takes the first free spot, scanning from the lower left.
    title.style.width=width+40+'px';title.style.left='0';title.style.top='0';
    const tw=title.offsetWidth,th=title.offsetHeight;
    let spot=null;
    for(let y=H-margin-th;y>=margin&&!spot;y-=24)for(let x=margin+12;x+tw<=W-margin&&!spot;x+=36){
      const box={l:x,t:y,r:x+tw,b:y+th};
      if(blocked.some(b=>overlaps(box,b))||placedNotes.some(n=>overlaps(box,n,14))||placedSegments.some(s=>segmentHitsRect(s,box)))continue;
      spot=box;
    }
    if(!spot){map.dataset.fallback='no place for the title';return false;}
    title.style.left=spot.l+'px';title.style.top=spot.t+'px';
    items.forEach(item=>{
      const p=item.place;item.el.classList.toggle('end',p.end);
      item.el.style.left=p.x+'px';item.el.style.top=p.y+'px';
      svg('path',{class:'help-map-leader','data-note':item.note.id,d:pathOf(p.points)},marks);
      svg('circle',{class:'help-map-dot',cx:p.dot[0],cy:p.dot[1],r:2.4},marks);
    });
    return true;
  }
  function place(force){
    if(!open)return;
    const items=shown();
    const key=root.innerWidth+'x'+root.innerHeight+'|'+items.map(i=>i.note.id+':'+[i.r.l,i.r.t,i.r.r,i.r.b].map(Math.round).join(',')).join('|');
    if(!force&&key===lastKey)return;
    lastKey=key;
    art.setAttribute('width',root.innerWidth);art.setAttribute('height',root.innerHeight);
    veil.setAttribute('width',root.innerWidth);veil.setAttribute('height',root.innerHeight);
    noteEls.forEach((el,id)=>{el.hidden=!items.some(item=>item.note.id===id);});
    map.classList.remove('compact');delete map.dataset.fallback;drawFrames(items);
    if(!annotate(items)){
      [title,...noteEls.values()].forEach(el=>{el.style.left=el.style.top=el.style.width='';el.classList.remove('end');});
      map.classList.add('compact');drawFrames(items);
    }
  }
  let watch=0;
  // opener: the button that opened the map, which gets focus back on close
  // (a click does not focus a button in every browser).
  function show(opener){
    // Already open: lay it out again, keep where focus returns to.
    if(open){lastKey='';place(true);return;}
    open=true;previousFocus=opener&&opener.isConnected?opener:doc.activeElement;map.hidden=false;buttons.forEach(b=>b.setAttribute('aria-pressed','true'));
    lastKey='';place(true);map.querySelector('.help-map-close').focus();
    // The network settles and the deck streams in; follow the controls while open.
    clearInterval(watch);watch=setInterval(()=>place(false),700);
  }
  function hide(){
    if(!open)return;
    open=false;map.hidden=true;buttons.forEach(b=>b.setAttribute('aria-pressed','false'));clearInterval(watch);
    // Back to where the reader was; else to the way in they can reach now
    // (the header's button is under the panel while it is open).
    if(previousFocus&&previousFocus.isConnected&&previousFocus!==doc.body)previousFocus.focus();
    else (panelOpen()?panelButton:button).focus();
  }
  buttons.forEach(b=>b.addEventListener('click',()=>{if(open)hide();else show(b);}));
  map.querySelector('.help-map-close').addEventListener('click',hide);
  // Anywhere outside the compact legend closes the map; the legend itself scrolls.
  map.addEventListener('click',event=>{
    if(event.target.closest('.help-map-close'))return;
    if(!(map.classList.contains('compact')&&event.target.closest('.help-map-notes,.help-map-title')))hide();
  });
  doc.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&open){event.preventDefault();event.stopImmediatePropagation();hide();}
  },true);
  let frame=0;
  const soon=()=>{cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>place(true));};
  root.addEventListener('resize',soon);
  root.addEventListener('scroll',()=>{if(open)soon();},true);
  API.show=show;API.hide=hide;API.place=()=>place(true);
}
if(doc.readyState==='loading')doc.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})(typeof window==='undefined'?globalThis:window);
