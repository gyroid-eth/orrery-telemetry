/* The cockpit's Full tour, pointing inside this page.
 *
 * The cockpit's tour rings the control to press next (gyroid-eth/orrery
 * bridge/cockpit_tour.js). Its later steps happen in here, the Telemetry page
 * embedded in the cockpit, which the cockpit cannot reach into: it sends the
 * step instead ({type:'orrery-tour-cue', version:1, step, avoid}), and this
 * page draws the same cyan ring and HERE tag on its own control. The step
 * names a kind of control, not an agent: "EXIT" rings the first EXIT on
 * screen, which need not be the one the step's text means.
 */
(function(root){
'use strict';
// Each step's controls, in order; the first one showing is ringed. A later
// entry is the way to the control (the view toggle, a card to open). An
// entry [selector, text] also needs its label to match: the panel's one
// button reads RESUME IN COCKPIT for an ended agent and OPEN IN COCKPIT for
// a live one, and only one of them is the step's action.
const STEPS=Object.freeze({
  'full-exit':['.bay .exitbtn','.viewtog'],
  'full-edge':['#net .edge-count','.viewtog'],
  'full-select':['#selToggle','.viewtog'],
  'full-replay':['#selbarReplay','#selToggle','.viewtog'],
  // An ended agent shows outside LIVE: its card, else the history range.
  // A panel open on the other kind of agent is closed first (#tm-x).
  'full-resume':[['#tm-open','RESUME'],'#selbarResume','.bay.cat-gone .top','#history','#tm-x'],
  'full-network-settings':['#settings-btn','.viewtog'],
  'full-return':[['#tm-open','OPEN IN COCKPIT'],'.bay.cat-agent .top','#tm-x'],
});
const own=step=>typeof step==='string'&&Object.prototype.hasOwnProperty.call(STEPS,step);
// Where the HERE tag sits beside the ring: below, above, right or left, inside
// the view and clear of what it must not cover; null when no side has room.
// The same rule as the cockpit's placeHereTag.
function placeHereTag(r,size,view,avoid=[],gap=8,margin=6){
  const cx=(r.l+r.r)/2,cy=(r.t+r.b)/2;
  const sides=[
    ['below',cx-size.w/2,r.b+gap],['above',cx-size.w/2,r.t-gap-size.h],
    ['right',r.r+gap,cy-size.h/2],['left',r.l-gap-size.w,cy-size.h/2]];
  for(const [side,x0,y0] of sides){
    const x=side==='below'||side==='above'?Math.max(margin,Math.min(view.w-margin-size.w,x0)):x0;
    const y=side==='left'||side==='right'?Math.max(margin,Math.min(view.h-margin-size.h,y0)):y0;
    const box={l:x,t:y,r:x+size.w,b:y+size.h};
    if(box.l<margin||box.t<margin||box.r>view.w-margin||box.b>view.h-margin)continue;
    if(avoid.some(a=>a&&box.l<a.r&&box.r>a.l&&box.t<a.b&&box.b>a.t))continue;
    return {x,y,side};
  }
  return null;
}
const API={STEPS,placeHereTag};
if(typeof module!=='undefined'&&module.exports)module.exports=API;
root.TelemetryTourCue=API;
const doc=root.document;
if(!doc)return;

// The network's edge counts carry no pair of their own; the page's badge list
// (gEls.badge: {tx, s, t}) knows which pair each one belongs to, and each hit
// line carries its pair in data-s / data-t.
function ownHitLine(el,top){
  if(!el.classList||!el.classList.contains('edge-count')||!top.classList||!top.classList.contains('edge-hit'))return false;
  // A classic script sees the page's top-level gEls; a page without it has no edges.
  /* global gEls */
  const badges=typeof gEls!=='undefined'&&gEls?gEls.badge:null;
  const badge=Array.isArray(badges)&&badges.find(item=>item.tx===el);
  if(!badge)return false;
  const s=top.dataset.s,t=top.dataset.t;
  return (s===badge.s&&t===badge.t)||(s===badge.t&&t===badge.s);
}
function showing(el){
  // A control that cannot be pressed now is not the next one to press.
  if(el.disabled||el.getAttribute('aria-disabled')==='true')return null;
  const b=el.getBoundingClientRect(),W=root.innerWidth,H=root.innerHeight;
  if(b.width<2||b.height<2||b.right<=0||b.bottom<=0||b.left>=W||b.top>=H)return null;
  const cs=root.getComputedStyle(el);
  if(cs.visibility==='hidden'||cs.display==='none'||+cs.opacity===0)return null;
  // Under anything else (a drawer, a dialog, a mail card passing over the
  // network): not what can be pressed now. One exception: an edge's count
  // lies under that edge's own wider hit line, which takes the edge's clicks.
  const x=Math.min(W-1,Math.max(0,(b.left+b.right)/2)),y=Math.min(H-1,Math.max(0,(b.top+b.bottom)/2));
  const top=doc.elementFromPoint(x,y);
  if(top&&top!==el&&!el.contains(top)&&!ownHitLine(el,top))return null;
  return b;
}
// The first control of the step that is on screen and not covered.
function pick(step){
  if(!own(step))return null;
  for(const entry of STEPS[step]){
    const [sel,text]=Array.isArray(entry)?entry:[entry,null];
    for(const el of doc.querySelectorAll(sel)){
      if(text&&!(el.textContent||'').toUpperCase().includes(text))continue;
      const b=showing(el);if(b)return {el,b};
    }
  }
  return null;
}
API.pick=step=>{const p=pick(step);return p&&p.el;};

function mount(){
  const ring=doc.createElement('div');ring.className='tour-cue-ring';ring.hidden=true;ring.setAttribute('aria-hidden','true');
  const here=doc.createElement('div');here.className='tour-cue-here';here.hidden=true;here.setAttribute('aria-hidden','true');
  doc.body.append(ring,here);
  let step=null,avoid=[],timer=0;
  const label=side=>side==='below'?'▲ HERE':side==='above'?'▼ HERE':side==='right'?'◀ HERE':'HERE ▶';
  function place(){
    const hide=()=>{ring.hidden=true;here.hidden=true;delete ring.dataset.target;};
    const found=step&&pick(step);
    if(!found)return hide();
    const {el,b}=found,W=root.innerWidth,H=root.innerHeight,pad=4;
    const r={l:Math.max(2,b.left-pad),t:Math.max(2,b.top-pad),r:Math.min(W-2,b.right+pad),b:Math.min(H-2,b.bottom+pad)};
    Object.assign(ring.style,{left:r.l+'px',top:r.t+'px',width:(r.r-r.l)+'px',height:(r.b-r.t)+'px'});
    ring.hidden=false;ring.dataset.target=el.id||el.getAttribute('class')||el.tagName.toLowerCase();
    // Measure the finished tag; settle when its side gives back its arrow.
    here.hidden=false;here.style.visibility='hidden';here.textContent=here.textContent||label('below');
    let spot=null;
    for(let i=0;i<3;i++){
      spot=placeHereTag(r,{w:here.offsetWidth,h:here.offsetHeight},{w:W,h:H},avoid);
      if(!spot||label(spot.side)===here.textContent)break;
      here.textContent=label(spot.side);
    }
    if(!spot||label(spot.side)!==here.textContent){here.hidden=true;here.style.visibility='';return;}
    here.dataset.side=spot.side;
    Object.assign(here.style,{left:spot.x+'px',top:spot.y+'px',visibility:''});
  }
  function set(next,rects){
    step=own(next)?next:null;
    avoid=Array.isArray(rects)?rects.filter(a=>a&&[a.l,a.t,a.r,a.b].every(Number.isFinite)):[];
    clearInterval(timer);place();
    // Cards stream in and views switch without a tour change: follow them.
    if(step)timer=setInterval(place,700);
  }
  root.addEventListener('message',event=>{
    const d=event.data;
    if(event.origin!==root.location.origin||event.source!==root.parent||!d||d.type!=='orrery-tour-cue'||d.version!==1)return;
    set(typeof d.step==='string'?d.step:null,d.avoid);
  });
  root.addEventListener('resize',place);
  root.addEventListener('scroll',place,true);
  API.set=set;API.place=place;
}
// Only inside the cockpit: a page of its own has no tour to follow.
if(root.parent===root)return;
if(doc.readyState==='loading')doc.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})(typeof window==='undefined'?globalThis:window);
