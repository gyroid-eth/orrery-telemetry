(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.AgentStackColorThemeCore=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  const clamp=value=>Math.min(1,Math.max(0,value));
  const DARK=Object.freeze({
    canvas:{L:.15873,C:.00913,H:264.27},panel:{L:.19034,C:.01122,H:260.65},
    strong:{L:.21685,C:.01509,H:261.62},elevated:{L:.24680,C:.01871,H:262.15},
    terminal:{L:.15402,C:.00920,H:264.28},primary:{L:.92334,C:.02141,H:85.95},
    secondary:{L:.66497,C:.02312,H:85.96},muted:{L:.47444,C:.01612,H:93.15},
    accent:{L:.81420,C:.12890,H:75.80},local:{L:.70914,C:.08583,H:179.83},
    remote:{L:.70857,C:.12807,H:306.10},delegate:{L:.70758,C:.10845,H:135.04},
    alert:{L:.70795,C:.18434,H:27.69},question:{L:.85564,C:.13791,H:208.39},
  });
  const WARM=Object.freeze({canvas:{L:.95301,C:.01964,H:87.51},depthScale:1.48,
    depthChroma:.145,depthHue:-34,terminalLift:.017});

  function linear(color){
    const radians=color.H*Math.PI/180;
    const a=color.C*Math.cos(radians),b=color.C*Math.sin(radians);
    const l_=color.L+.3963377774*a+.2158037573*b;
    const m_=color.L-.1055613458*a-.0638541728*b;
    const s_=color.L-.0894841775*a-1.291485548*b;
    const l=l_**3,m=m_**3,s=s_**3;
    return [4.0767416621*l-3.3077115913*m+.2309699292*s,
      -1.2684380046*l+2.6097574011*m-.3413193965*s,
      -.0041960863*l-.7034186147*m+1.707614701*s];
  }
  const inGamut=rgb=>rgb.every(channel=>channel>=0&&channel<=1);
  function encode(channel){
    const value=channel<=.0031308?12.92*channel:1.055*channel**(1/2.4)-.055;
    return Math.round(clamp(value)*255);
  }
  function fit(color){
    let candidate={...color},rgb=linear(candidate);
    if(!inGamut(rgb)){
      let low=0,high=Math.max(0,color.C);
      for(let index=0;index<30;index+=1){
        const chroma=(low+high)/2,probe={...color,C:chroma};
        if(inGamut(linear(probe)))low=chroma;else high=chroma;
      }
      candidate={...color,C:low};rgb=linear(candidate);
    }
    return Object.freeze({oklch:Object.freeze(candidate),rgb:Object.freeze(rgb.map(encode))});
  }
  function luminance(rgb){
    const decoded=rgb.map(channel=>{const value=channel/255;
      return value<=.04045?value/12.92:((value+.055)/1.055)**2.4;});
    return .2126*decoded[0]+.7152*decoded[1]+.0722*decoded[2];
  }
  function contrast(first,second){
    const a=luminance(first),b=luminance(second);
    return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
  }
  function solve(seed,background,target){
    const hardTarget=target+.02,baseline=fit(seed);
    if(contrast(baseline.rgb,background)>=hardTarget)return baseline;
    let low=0,high=seed.L,best=fit({...seed,L:0});
    for(let index=0;index<32;index+=1){
      const lightness=(low+high)/2,candidate=fit({...seed,L:lightness});
      if(contrast(candidate.rgb,background)>=hardTarget){best=candidate;low=lightness;}
      else high=lightness;
    }
    return best;
  }
  function surface(role){
    const depth=(DARK[role].L-DARK.canvas.L)*WARM.depthScale;
    return fit({L:Math.min(.985,WARM.canvas.L-depth+(role==='terminal'?WARM.terminalLift:0)),
      C:Math.max(0,WARM.canvas.C+WARM.depthChroma*depth),
      H:(WARM.canvas.H+WARM.depthHue*depth+360)%360});
  }
  const hex=rgb=>'#'+rgb.map(channel=>channel.toString(16).padStart(2,'0')).join('');
  const rgba=(rgb,alpha)=>`rgb(${rgb.join(' ')} / ${alpha})`;

  function deriveWarmPaperLightTheme(){
    const roles={canvas:surface('canvas'),panel:surface('panel'),strong:surface('strong'),
      elevated:surface('elevated'),terminal:surface('terminal')};
    const darkest=roles.elevated.rgb;
    for(const [role,target] of Object.entries({primary:12,secondary:7,muted:4.5,
      accent:4.5,local:4.5,remote:4.5,delegate:4.5,alert:4.5,question:4.5})){
      roles[role]=solve(DARK[role],darkest,target);
    }
    roles.control=solve(DARK.muted,darkest,3);
    const cssVariables={
      '--bg':hex(roles.canvas.rgb),'--panel':hex(roles.panel.rgb),
      '--panel-2':hex(roles.strong.rgb),'--elev':hex(roles.elevated.rgb),
      '--ink':hex(roles.primary.rgb),'--ink-dim':hex(roles.secondary.rgb),
      '--ink-faint':hex(roles.muted.rgb),'--amber':hex(roles.accent.rgb),
      '--amber-deep':hex(roles.accent.rgb),'--amber-glow':'transparent',
      '--ln-local':hex(roles.local.rgb),'--ln-remote':hex(roles.remote.rgb),
      '--ln-delegate':hex(roles.delegate.rgb),'--alert':hex(roles.alert.rgb),
      '--cyan':hex(roles.question.rgb),'--void':hex(roles.canvas.rgb),
      '--bone':hex(roles.primary.rgb),'--bone-dim':hex(roles.secondary.rgb),
      '--line':rgba(roles.primary.rgb,.16),'--line-soft':rgba(roles.primary.rgb,.08),
      '--hair':rgba(roles.primary.rgb,.16),'--hair-2':rgba(roles.primary.rgb,.08),
      '--glow-a':'none','--glow-c':'none',
      '--theme-border-control':hex(roles.control.rgb),
      '--theme-surface-terminal':hex(roles.terminal.rgb),
      '--theme-shadow-low':rgba(roles.primary.rgb,.12),
      '--theme-shadow-high':rgba(roles.primary.rgb,.08),
      '--theme-ink-rgb':roles.primary.rgb.join(' '),
      '--theme-accent-rgb':roles.accent.rgb.join(' '),
    };
    return Object.freeze({id:'warm-paper',mode:'light',roles:Object.freeze(roles),
      cssVariables:Object.freeze(cssVariables)});
  }
  function contrastReport(theme=deriveWarmPaperLightTheme()){
    const background=theme.roles.elevated.rgb;
    return Object.freeze(Object.fromEntries(Object.entries({primary:12,secondary:7,muted:4.5,
      accent:4.5,local:4.5,remote:4.5,delegate:4.5,alert:4.5,question:4.5,control:3})
      .map(([role,target])=>[role,Object.freeze({ratio:contrast(theme.roles[role].rgb,background),target})])));
  }
  return Object.freeze({DARK,WARM,fit,contrast,deriveWarmPaperLightTheme,contrastReport});
});
