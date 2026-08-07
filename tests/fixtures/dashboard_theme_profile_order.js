(async()=>{
  const waitFrame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  await document.fonts.ready;await waitFrame();
  const request=(axis,value)=>applyThemeAxisMessage({
    type:'agentstack-theme-axis',version:1,axis,value});
  const reset=async()=>{
    const state=window.AGENTSTACK_THEME_AXIS_STATE();
    if(state.axis)await request(state.axis,null);
  };
  const result={normal:[],zero:[],minimum:[],adversarial:{}};

  for(const axis of THEME_AXIS_NAMES){
    for(const value of [.25,.5,.75,1]){
      await reset();result.normal.push({axis,value,...await request(axis,value)});
    }
  }
  await reset();
  for(const axis of THEME_AXIS_NAMES){
    result.zero.push({axis,...await request(axis,0)});await reset();
  }
  for(const axis of THEME_AXIS_NAMES){
    result.minimum.push({axis,...await request(axis,Number.MIN_VALUE)});await reset();
  }

  const cssProperty=property=>property.replace(/[A-Z]/g,
    letter=>`-${letter.toLowerCase()}`);
  const visibleTokenTarget=()=>{
    const entries=captureThemeAxisTokenEffectEntries('dim-contrast');
    const index=entries.findIndex(entry=>entry.inViewport&&!entry.target.pseudo&&
      entry.target.element!==document.documentElement&&entry.properties.length>0);
    if(index<0)throw new Error('no visible generated token consumer');
    return {entries,index,entry:entries[index]};
  };
  const installOverride=(entry,values,label)=>{
    const element=entry.target.element;
    const oldId=element.getAttribute('id');
    element.id=`dashboard-theme-${label}`;
    const style=document.createElement('style');
    style.dataset.dashboardThemeAdversarial=label;
    style.textContent=`#${element.id}{${entry.properties.map((property,index)=>
      `${cssProperty(property)}:${values[index]}!important`).join(';')}}`;
    document.head.appendChild(style);
    return ()=>{
      style.remove();
      if(oldId===null)element.removeAttribute('id');else element.id=oldId;
    };
  };

  /* B=1 membership is eligibility-only. A member already at the requested
     endpoint remains in the denominator and counts as reached even though it
     is not part of the changed set. */
  {
    await reset();
    const {entries,index,entry}=visibleTokenTarget();
    const expected=themeAxisExpectedEffectValues('dim-contrast',entries,1)[index];
    const beforeMembership=entries.length;
    const remove=installOverride(entry,expected,'already-endpoint');
    const afterMembership=captureThemeAxisTokenEffectEntries('dim-contrast').length;
    const applied=await request('dim-contrast',1);
    result.adversarial.alreadyAtEndpoint={beforeMembership,afterMembership,
      expected,applied};
    await request('dim-contrast',null);remove();
  }

  /* A higher-specificity post-cascade override also creates the pre-apply
     computed/token mismatch. Neither fact may shrink generated membership;
     the request must fail attainment and restore the complete last-valid
     state. */
  {
    const lastValid=await request('dim-contrast',.25);
    const {entries,entry}=visibleTokenTarget();
    const beforeMembership=entries.length;
    const blockedValues=entry.properties.map(()=> 'rgb(1, 2, 3)');
    const remove=installOverride(entry,blockedValues,'important-blocked');
    const computedMismatch=entry.properties.map(property=>
      getComputedStyle(entry.target.element)[property]);
    const afterMembership=captureThemeAxisTokenEffectEntries('dim-contrast').length;
    const rejected=await request('dim-contrast',1);
    const rolledBackState=window.AGENTSTACK_THEME_AXIS_STATE();
    remove();await waitFrame();
    const afterRemoval=entry.properties.map(property=>
      getComputedStyle(entry.target.element)[property]);
    result.adversarial.importantAndPreapplyMismatch={lastValid,beforeMembership,
      afterMembership,computedMismatch,rejected,rolledBackState,afterRemoval};
    await request('dim-contrast',null);
  }

  const hidden=document.createElement('style');
  hidden.textContent='html,body,body *{visibility:hidden!important}';
  document.head.appendChild(hidden);
  result.adversarial.hidden=await request('small-text',.5);
  hidden.remove();await reset();

  result.final=window.AGENTSTACK_THEME_AXIS_STATE();
  return result;
})()
