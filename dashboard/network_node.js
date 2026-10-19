/* Shared medallion renderer for the legacy and global network. */
(function(){
'use strict';
const NR=13,SVGNS='http://www.w3.org/2000/svg';
function svgEl(t,attrs){const e=document.createElementNS(SVGNS,t);for(const [k,v] of Object.entries(attrs||{}))e.setAttribute(k,v);return e;}
const SCIENTISTS=["Leeuwenhoek","Boltzmann","Arrhenius","Ramanujan",
  "Langmuir","Guericke","Vesalius","Faraday","Feynman","Einstein","Pasteur",
  "Linnaeus","Ostwald","Maxwell","Pascal","Newton","Planck","Kepler",
  "Mendel","Turing","Hubble","Darwin","Tesla","Curie","Euler","Gauss",
  "Hooke","Bohr","Fabre",
  // Added 2026-06-26 (round 2) — keep in sync with SCIENTIST_NOUNS in utils.py
  "Copernicus","Archimedes","Mendeleev","Franklin","Galileo","Edison",
  "Dirac","Fermi","Koch","Bell",
  // Added 2026-06-26 (round 3)
  "Somerville","Lavoisier","Lovelace","Goodall","Pauling","Noether",
  "Yukawa","Hopper","Lamarr","Bose","Watt"].sort((a,b)=>b.length-a.length);
function scientistOf(name){
  for(const s of SCIENTISTS)
    if(name.toLowerCase().endsWith(s.toLowerCase()))return s;
  return null;
}
function portURL(sci,hi){
  /* Portraits come from the server, which resolves a name to a file. A
     static build has no server, and <img src> never reaches the demo's
     fetch shim, so without this hook every face falls back to initials —
     the page still works and quietly looks like a different product. */
  if(window.AGENTSTACK_DEMO&&window.AGENTSTACK_DEMO.portraitURL)
    return window.AGENTSTACK_DEMO.portraitURL(sci,hi);
  return `/portrait?name=${encodeURIComponent(sci)}${hi?'&hi=1':''}`;
}
function portraitFallback(el){
  if(!el||el.dataset.portraitFallback==='1'){
    if(el){el.hidden=true;el.style.display='none';}
    return;
  }
  el.dataset.portraitFallback='1';
  const u=new URL(el.getAttribute('src')||el.getAttribute('href')||'',location.href);
  u.searchParams.delete('hi');
  if(el.tagName&&el.tagName.toLowerCase()==='image')el.setAttribute('href',u.pathname+u.search);
  else el.src=u.pathname+u.search;
}
// provider (AI 提供元) をメダリオン右下の丸座金に載せる。
// 公式ロゴ SVG（bone カラーで再着色済み）は /assets/ から配信。
// 不明 provider は null（呼び出し側で skip）。
const _PROVIDER_ASPECT = {
  // 公式 SVG の viewBox から: w/h
  // anthropic key は Claude ブランドロゴ（24x24 正方形）を表示
  anthropic: 1,         // 24x24 正方形（Claude logo）
  openai:    256/260,   // ~0.98 (ほぼ正方形)
  google:    1,         // 24x24 正方形（Gemini logo）
  deepseek:  71.69/52.76,
  qwen:      1,
};
// asset の更新が SVG <image> 要素のキャッシュに引っかかるため、ページごとに
// 一意な version 文字列を付けて確実に最新を取得する（page load ごとに 1 回）。
const _ASSET_V = Date.now().toString(36);
// Asset paths are absolute so the dashboard works wherever the server maps
// it. A static demo build has no server and may sit in a subdirectory, so it
// is allowed to resolve them itself — same hook shape as portURL.
function assetURL(name){
  if(window.AGENTSTACK_DEMO&&window.AGENTSTACK_DEMO.assetURL)
    return window.AGENTSTACK_DEMO.assetURL(name,_ASSET_V);
  return `/assets/${name}.svg?v=${_ASSET_V}`;
}
function providerKeyFor(g){
  const raw=String((g&&g.provider)||'').toLowerCase();
  // localは共通profileの投影だけを受ける。engine名やURLから推測しない。
  if(raw==='local'){
    const model=String(g.model_raw||g.model||'').toLowerCase();
    if(/(?:^|[\/\s_-])qwen/.test(model))return 'local:qwen';
    if(/(?:^|[\/\s_-])deepseek/.test(model)&&window.AGENTSTACK_DSH_ENABLED===true)
      return 'local:deepseek';
    return 'local';
  }
  if(_PROVIDER_ASPECT[raw]&&raw!=='qwen'&&
     (raw!=='deepseek'||window.AGENTSTACK_DSH_ENABLED===true))return raw;
  const model=String((g&&g.model)||'').toLowerCase();
  if(/claude|opus|sonnet|haiku|fable|mythos/.test(model))return 'anthropic';
  if(/codex|gpt/.test(model))return 'openai';
  return '';
}
function providerBadge(provider,x,y,size=14){
  const local=provider==='local'||provider.startsWith('local:');
  const maker=local?provider.split(':')[1]:provider;
  const ar=_PROVIDER_ASPECT[maker];
  if(!ar&&!local)return null;
  const badge=svgEl('g',{class:'provider-badge'+(local?' provider-local':''),
    transform:`translate(${x.toFixed(2)} ${y.toFixed(2)})`});
  if(local){
    const r=size/2;
    const points=Array.from({length:6},(_,i)=>{
      const a=Math.PI*i/3;return `${(r*Math.cos(a)).toFixed(2)},${(r*Math.sin(a)).toFixed(2)}`;
    }).join(' ');
    badge.appendChild(svgEl('polygon',{class:'provider-seat provider-local-ring',points}));
  }else badge.appendChild(svgEl('circle',{class:'provider-seat',r:(size/2).toFixed(2)}));
  if(!ar)return badge;
  const h=size*.58,w=h*ar;
  badge.appendChild(svgEl('image',{class:'provider-logo','data-provider':maker,
    href:assetURL(maker),
    x:(-w/2).toFixed(2), y:(-h/2).toFixed(2),
    width:w.toFixed(2), height:h.toFixed(2),
    preserveAspectRatio:'xMidYMid meet'}));
  return badge;
}

function renderLiveStateBubble(grp,actState){
  // buildEls normally creates a fresh node group, but explicit removal keeps
  // the helper correct if a caller ever updates one group in place.
  grp.querySelectorAll('.askbub,.qbub').forEach(n=>n.remove());
  const question=actState==='question';
  const ask=actState==='ask';
  if(!question&&!ask)return null;
  const bubble=svgEl('g',{
    class:question?'qbub':'askbub',
    role:'img',
    'aria-label':question?'agent question':'approval requested'
  });
  // 軌道環はノードを囲む。字はその右上、環の外周に載せる。
  //
  // This ring sits at NR*1.18 = 15.3, inside the context arc at NR+5 = 18,
  // and that is correct. It was moved out to NR+8 for a while on the theory
  // that the gauge was burying it; the theory was wrong. The ring was hard
  // to see because it was dashed and drew 24% of its circumference, not
  // because anything covered it. Drawn as a full circle it reads clearly
  // from inside the gauge, so the radius came back.
  //
  // The lesson is the cheap test that was skipped: remove the dash and look.
  // Two numbers agreeing with a story is not the same as the story being
  // true.
  bubble.appendChild(svgEl('circle',{
    class:'bubo',cx:0,cy:0,r:(NR*1.18).toFixed(2),pathLength:'100'
  }));
  const glyph=svgEl('text',{
    class:question?'bq':'bx',
    x:(NR*.84).toFixed(2),y:(-NR*.84).toFixed(2),'text-anchor':'middle'
  });
  glyph.textContent=question?'?':'!';
  bubble.appendChild(glyph);
  grp.appendChild(bubble);
  return bubble;
}

function render(name,g,{selected=false,avatar=scientistOf}={}){
    const st=g.retired?'retired'
      :(g.state||(g.running?'run':g.present?'finished':'gone'));
    const selCls=selected?' selnode':'';
    const cls=`node ${st} sp-${g.role}${selCls}`;
    const grp=svgEl('g',{class:cls});
    grp.appendChild(svgEl('circle',{class:'node-halo',r:NR+11}));
    grp.appendChild(svgEl('circle',{class:'ring',r:NR+1}));
    if(st==='run'){
      const aw=g.actState||'wait';
      grp.classList.add('act-'+aw);
    }
    if(typeof g.ctxUsed==='number'){
      const CTXR=NR+5,C=2*Math.PI*CTXR,maxArc=C*.75;
      const rem=Math.max(0,Math.min(1,(100-g.ctxUsed)/100));
      const gap=C-maxArc;
      const track=svgEl('circle',{class:'ctx-track',r:CTXR});
      track.setAttribute('stroke-dasharray',
        `${maxArc.toFixed(2)} ${gap.toFixed(2)}`);
      grp.appendChild(track);
      const arcClass=rem<.2?' ctx-low':'';
      const arcDash=
        `${(maxArc*rem).toFixed(2)} ${(C-maxArc*rem).toFixed(2)}`;
      const arcGlow=svgEl('circle',{
        class:'ctx-arc-glow'+arcClass,r:CTXR,
        'data-remaining':Math.round(rem*100)});
      arcGlow.setAttribute('stroke-dasharray',arcDash);
      grp.appendChild(arcGlow);
      const arc=svgEl('circle',{
        class:'ctx-arc'+arcClass,r:CTXR,
        'data-remaining':Math.round(rem*100)});
      arc.setAttribute('stroke-dasharray',arcDash);
      grp.appendChild(arc);
    }
    grp.appendChild(svgEl('circle',{
      class:'motion-ring',r:NR+9,pathLength:100}));
    const pkey=avatar(name);
    if(pkey){
      const img=svgEl('image',{class:'pimg',href:portURL(pkey,true),
        x:-NR,y:-NR,width:2*NR,height:2*NR,
        'clip-path':'url(#pclip)',
        preserveAspectRatio:'xMidYMid slice'});
      img.addEventListener('error',()=>portraitFallback(img));
      grp.appendChild(img);
      grp.appendChild(svgEl('circle',{class:'portrait-shade',r:NR}));
      grp.appendChild(svgEl('circle',{class:'prole',r:NR,fill:'none'}));
    }else{
      const rad=g.running?8:g.deg>3?7:6;
      grp.appendChild(svgEl('circle',{class:'dot',r:rad}));
    }
    const engineProvider=providerKeyFor(g);
    if(engineProvider){
      const badge=providerBadge(engineProvider,NR*.72,NR*.72,14);
      if(badge){
        badge.insertBefore(svgEl('circle',{
          class:'provider-seat-shadow',cy:1.5,r:7}),badge.firstChild);
        grp.appendChild(badge);
      }
    }
    renderLiveStateBubble(grp,g.actState);
    const tx=svgEl('text',{x:0,y:NR+16,'text-anchor':'middle'});
    tx.textContent=name;
    if(g.requested_name){
            tx.setAttribute('class','nlab-subst');
      const st=svgEl('title');
      st.textContent='requested '+g.requested_name+', registered as '+name;
      grp.appendChild(st);
    }
    grp.appendChild(tx);
    const annotLab=g.annot?((g.annot.emoji?g.annot.emoji+' ':'')+(g.annot.role||'')).trim():'';
    const hasAnnot=!!annotLab;
    if(hasAnnot){
      const lab=annotLab;
      const ay=NR+29;
      const at=svgEl('text',{class:'annot-lab',x:0,y:ay,'text-anchor':'middle'});
      at.textContent=lab;
      grp.appendChild(at);
      const tw=at.getComputedTextLength()||(lab.length*6.2);
      const padX=5;
      const ar=svgEl('rect',{class:'annot-pill',
        x:(-tw/2-padX).toFixed(1),y:(ay-9.5).toFixed(1),
        width:(tw+padX*2).toFixed(1),height:12.5,rx:6.2,ry:6.2});
      grp.insertBefore(ar,at);   // pill を text の後ろ（下層）へ
    }
    if(g.mail_status==='unavailable'){
      const mailLabel=svgEl('text',{x:0,y:NR+(hasAnnot?43:30),'text-anchor':'middle',
        style:'font-family:var(--mono);font-size:8.5px;fill:var(--ink-dim)'});
      mailLabel.textContent='MAIL UNAVAILABLE';
      const mailTitle=svgEl('title');
      mailTitle.textContent=(g.mail_message||'This agent cannot send or receive ORRERY Mail')+' · '+(g.mail_reason||'');
      mailLabel.appendChild(mailTitle);grp.appendChild(mailLabel);
    }
    if(g.provider_stall){
      const label=svgEl('text',{x:0,y:-NR-16,
        'text-anchor':'middle',style:'font-family:var(--mono);font-size:8.5px;fill:var(--alert)'});
      label.textContent='CAPACITY';
      const title=svgEl('title');
      title.textContent=g.provider_stall.error+' · '+g.provider_stall.notification_status;
      label.appendChild(title);grp.appendChild(label);
    }
    return grp;
}
window.OrreryNetworkNode={render,scientistOf,portURL,portraitFallback,providerKeyFor,providerBadge,assetURL,renderLiveStateBubble};
})();
