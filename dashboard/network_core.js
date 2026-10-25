/* Shared PC/mobile NETWORK primitives. No network, storage or operation APIs. */
(function(root){
'use strict';
const SVGNS='http://www.w3.org/2000/svg',NR=13;
const defaults=Object.freeze({NSIZE:13,L:110,LWMUL:1,KR:2600,GR:.012,KS:.012});
const parameters=Object.freeze({
 NSIZE:{label:'Node size',min:6,max:28,step:1,apply:'repaint'},
 L:{label:'Link distance',min:40,max:400,step:5,apply:'force'},
 LWMUL:{label:'Link width',min:.5,max:3,step:.1,apply:'rebuild'},
 KR:{label:'Repel force',min:500,max:10000,step:100,apply:'force'},
 GR:{label:'Center gravity',min:0,max:.05,step:.001,apply:'force'},
 KS:{label:'Link spring',min:0,max:.15,step:.005,apply:'force'}
});
function tuning(bindings){
 const out={};
 for(const[k,p]of Object.entries(parameters))out[k]={...bindings[k],apply:p.apply,
  fmt:v=>k==='LWMUL'?'×'+v.toFixed(1):v.toFixed(k==='GR'||k==='KS'?3:0)};
 return out;
}
function svgEl(t,attrs){const e=document.createElementNS(SVGNS,t);for(const k in attrs)e.setAttribute(k,attrs[k]);return e;}
function createDefs(gsvg){
  const d=svgEl('defs');
  for(const[id,c]of[['mk-c','var(--amber)'],['mk-s','var(--amber)']]){
    const m=svgEl('marker',{id,viewBox:'0 0 10 10',refX:9,refY:5,
      markerWidth:6.5,markerHeight:6.5,orient:'auto-start-reverse'});
    m.appendChild(svgEl('path',{d:'M0 0L10 5L0 10z',fill:c,opacity:.85}));
    d.appendChild(m);
  }
  const halo=svgEl('radialGradient',{
    id:'node-halo-grad',cx:'50%',cy:'50%',r:'50%'});
  for(const[offset,opacity]of[['0%',.28],['50%',.2],['76%',.08],['100%',0]])
    halo.appendChild(svgEl('stop',{
      offset,'stop-color':'#ece5d6','stop-opacity':opacity}));
  d.appendChild(halo);
  const cometTail=svgEl('linearGradient',{
    id:'comet-tail-grad',gradientUnits:'userSpaceOnUse',
    x1:-29,y1:0,x2:0,y2:0});
  for(const[offset,color,opacity]of[
    ['0%','#f2b65a',0],['68%','#f2b65a',.3],['100%','#ece5d6',.88]])
    cometTail.appendChild(svgEl('stop',{
      offset,'stop-color':color,'stop-opacity':opacity}));
  d.appendChild(cometTail);
  const cometCore=svgEl('radialGradient',{
    id:'comet-core-grad',cx:'50%',cy:'50%',r:'50%'});
  for(const[offset,color,opacity]of[
    ['0%','#ece5d6',.75],['45%','#f2b65a',.32],['100%','#f2b65a',0]])
    cometCore.appendChild(svgEl('stop',{
      offset,'stop-color':color,'stop-opacity':opacity}));
  d.appendChild(cometCore);
  const cp=svgEl('clipPath',{id:'pclip'});
  cp.appendChild(svgEl('circle',{cx:0,cy:0,r:13}));
  d.appendChild(cp);
  gsvg.appendChild(d);
}
function buildElements(gsvg,gmap,gedges,gspawn,options){
  const LWMUL=options.widthMultiplier;
  [...gsvg.querySelectorAll('.edge,.edge-hit,.edge-count,.node')].forEach(n=>n.remove());
  const gEls={node:new Map(),edge:[],badge:[]};
  // kind 別 (to/cc/bcc) かつ方向別 (A→B / B→A) に分かれて来る gedges を
  // pair 単位 (向き非依存) で合計し、各 pair に 1 個だけバッジを出す。
  // /api/edge-messages も双方向集計なので、これで drawer の count と一致する。
  const pairKey=(s,t)=>s<t?s+'|'+t:t+'|'+s;
  const pairTotal=new Map();
  for(const e of gedges){
    const k=pairKey(e.source,e.target);
    pairTotal.set(k,(pairTotal.get(k)||0)+(e.count||0));
  }
  const mk=(e,cls,mid)=>{
    // hit-line: 透明・太め・先に append（z-order で可視 line の下に置く）
    const hit=svgEl('line',{class:'edge-hit'});
    hit.dataset.s=e.source; hit.dataset.t=e.target;
    hit.dataset.kind=cls.includes('spawn')?'spawn':'comm';
    if(options.onEdgeClick)hit.addEventListener('click',options.onEdgeClick);
    gsvg.appendChild(hit);
    const attrs={class:cls};
    if(mid)attrs['marker-end']=`url(#${mid})`;   // 通信のみ矢印
    const ln=svgEl('line',attrs);
    if(cls==='edge'){
      const sw=LWMUL*Math.max(1,Math.min(6,1+Math.log2((e.count||1)+1)));
      ln.setAttribute('stroke-width',sw.toFixed(2));
    }
    gsvg.appendChild(ln);
    gEls.edge.push({ln,hit,s:e.source,t:e.target,cls});
  };
  gedges.forEach(e=>mk(e,'edge','mk-c'));        // 通信: シアン+矢印
  gspawn.forEach(e=>mk(e,'edge spawn',null));    // 親子: 破線・矢印なし
  // 通信 edge の中点に件数バッジを 1 pair 1 個だけ出す（向き非依存）
  const badgeSeen=new Set();
  for(const e of gedges){
    const k=pairKey(e.source,e.target);
    if(badgeSeen.has(k))continue;
    badgeSeen.add(k);
    const total=pairTotal.get(k);
    if(!total)continue;
    const tx=svgEl('text',{class:'edge-count'});
    tx.textContent=total;
    gsvg.appendChild(tx);
    gEls.badge.push({tx,s:e.source,t:e.target});
  }
  // 系統(role): 親=spawn.source / 子=spawn.target / 両方=中間
  const PAR=new Set(gspawn.map(s=>s.source));
  const CHI=new Set(gspawn.map(s=>s.target));
  const runGrps=[];   // 稼働中ノードは最後に再appendして前面化（CK P2）
  // 介入待ち (?/!) はさらにその上へ。SVG に z-index は無く描画順=DOM順なので、
  // 後から描かれた隣接ノードの ctx-arc-glow やラベルが人間を呼ぶ合図を覆い隠す
  // 事故が実在した（Swift-Noether の `!` が Quiet-Franklin に塗り潰され、
  // 「昔は出ていたのに消えた」ように見えていた）。レイアウトは force-directed で
  // 毎回変わるため、この事故は再現したりしなかったりして目視検査をすり抜ける。
  const attnGrps=[];
  for(const[name,g]of gmap){
    g.role=PAR.has(name)&&CHI.has(name)?'both':
      PAR.has(name)?'parent':CHI.has(name)?'child':'none';
    const st=g.retired?'retired':(g.state||(g.running?'run':g.present?'finished':'gone'));
    const grp=options.render(name,g);
    if(st==='run'){
      runGrps.push(grp);
      if(g.actState==='ask'||g.actState==='question')attnGrps.push(grp);
    }
    for(const [event,callback] of Object.entries(options.events||{}))
      grp.addEventListener(event,ev=>callback(ev,name));
    gsvg.appendChild(grp);
    gEls.node.set(name,{grp});
  }
  // 稼働中ノードを最後に再append＝最前面（密グラフで埋もれない CK P2）
  runGrps.forEach(el=>gsvg.appendChild(el));
  // 人間を呼ぶ合図は常に最前面。どんな配置でも ?/! が隠れないことを保証する。
  attnGrps.forEach(el=>gsvg.appendChild(el));
  return gEls;
}

function step(gmap,gedges,gspawn,dims,params,simCool=1){
  const {w,h}=dims;
  const {L,KR,GR,KS}=params;
  const arr=[...gmap.values()];
  const SP=.026;   // spawn spring（調整対象外）。KR/KS/L/GR は可変グローバル
  for(let i=0;i<arr.length;i++){
    const a=arr[i];
    for(let j=i+1;j<arr.length;j++){
      const b=arr[j];
      let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy||1;
      if(d2<90000){
        const f=KR/d2, d=Math.sqrt(d2),
          ux=dx/d*f,uy=dy/d*f;
        if(!a.fixed){a.vx+=ux;a.vy+=uy;}
        if(!b.fixed){b.vx-=ux;b.vy-=uy;}
      }
    }
  }
  const spring=(s,t,k)=>{
    const a=gmap.get(s),b=gmap.get(t);if(!a||!b)return;
    let dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1,
      f=(d-L)*k,ux=dx/d*f,uy=dy/d*f;
    if(!a.fixed){a.vx+=ux;a.vy+=uy;}
    if(!b.fixed){b.vx-=ux;b.vy-=uy;}
  };
  gedges.forEach(e=>spring(e.source,e.target,KS));
  gspawn.forEach(e=>spring(e.source,e.target,SP));
  let maxMove=0;
  for(const g of arr){
    if(g.fixed)continue;
    const ox=g.x,oy=g.y;
    g.vx+=(w/2-g.x)*GR*0.04;
    g.vy+=(h/2-g.y)*GR*0.04;
    g.vx*=.86*simCool;g.vy*=.86*simCool;
    g.x+=Math.max(-18,Math.min(18,g.vx));
    g.y+=Math.max(-18,Math.min(18,g.vy));
    // 名前＋role pill まで含めて viewport 内に残すための安全余白。
    g.x=Math.max(76,Math.min(w-76,g.x));
    g.y=Math.max(36,Math.min(h-66,g.y));
    maxMove=Math.max(maxMove,Math.hypot(g.x-ox,g.y-oy));
  }
  return maxMove;
}

function paint(gmap,gEls,NSIZE=13){
  for(const o of gEls.edge){
    const a=gmap.get(o.s),b=gmap.get(o.t);if(!a||!b)continue;
    o.ln.setAttribute('x1',a.x);o.ln.setAttribute('y1',a.y);
    o.ln.setAttribute('x2',b.x);o.ln.setAttribute('y2',b.y);
    if(o.hit){
      o.hit.setAttribute('x1',a.x);o.hit.setAttribute('y1',a.y);
      o.hit.setAttribute('x2',b.x);o.hit.setAttribute('y2',b.y);
    }
  }
  for(const o of gEls.badge||[]){
    const a=gmap.get(o.s),b=gmap.get(o.t);if(!a||!b)continue;
    o.tx.setAttribute('x',(a.x+b.x)/2);
    o.tx.setAttribute('y',(a.y+b.y)/2);
  }
  const nsc=NSIZE/NR;   // ノード一括拡縮（リング/バッジ/ラベルごと <g> 全体に適用）
  for(const[nm,o]of gEls.node){
    const g=gmap.get(nm);if(g)o.grp.setAttribute('transform',
      `translate(${g.x.toFixed(1)} ${g.y.toFixed(1)})`
      +(nsc!==1?` scale(${nsc.toFixed(3)})`:''));
  }
}

function fit(gmap,dims,{minK=0.3,maxK=2,pad=64}={}){
  const K_MIN=minK,FIT_K_MAX=maxK,FIT_PAD=pad;
  const arr=[...gmap.values()];
  if(!arr.length)return;
  let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity;
  for(const g of arr){
    if(g.x<x0)x0=g.x; if(g.x>x1)x1=g.x;
    if(g.y<y0)y0=g.y; if(g.y>y1)y1=g.y;
  }
  x0-=FIT_PAD;y0-=FIT_PAD;x1+=FIT_PAD;y1+=FIT_PAD;
  const {w,h}=dims;
  const bw=Math.max(1,x1-x0), bh=Math.max(1,y1-y0);
  const k=Math.max(K_MIN,Math.min(FIT_K_MAX,Math.min(w/bw,h/bh)));
  // bbox 中心を画面中心に置く
  return {k,vx:(x0+x1)/2-w/(2*k),vy:(y0+y1)/2-h/(2*k)};
}

const api={defaults,parameters,tuning,svgEl,createDefs,buildElements,step,paint,fit};
root.OrreryNetworkCore=api;
if(typeof module!=='undefined'&&module.exports)module.exports=api;
})(typeof window!=='undefined'?window:globalThis);
