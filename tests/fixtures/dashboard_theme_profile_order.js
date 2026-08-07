(async()=>{
  const waitFrame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  await document.fonts.ready;await waitFrame();
  const request=(axis,value)=>applyThemeAxisMessage({
    type:'agentstack-theme-axis',version:1,axis,value});
  let profileRequestSequence=0;
  const values=(small=null,tracking=null)=>({
    'dim-contrast':null,'small-text':small,tracking,glow:null,background:null
  });
  const profile=async requested=>{
    const data={type:'agentstack-theme-profile',version:1,
      requestId:`dashboard-profile-${++profileRequestSequence}`,values:requested};
    const applied=await applyThemeProfileMessage(data);
    return {data,applied,envelope:themeProfileResultMessage(data,applied)};
  };
  const reset=()=>profile(values());
  const result={normal:[],zero:[],minimum:[],adversarial:{},profile:{}};
  result.profile.ready=await new Promise((resolve,reject)=>{
    const messages=[];
    const timeout=setTimeout(()=>{
      window.removeEventListener('message',onMessage);
      reject(new Error('theme bridge ready timeout'));
    },1000);
    const onMessage=event=>{
      if(!['agentstack-theme-axis-ready','agentstack-theme-profile-ready']
          .includes(event.data&&event.data.type))return;
      messages.push(event.data);
      if(messages.length===2){
        clearTimeout(timeout);window.removeEventListener('message',onMessage);
        resolve(messages);
      }
    };
    window.addEventListener('message',onMessage);
    notifyThemeBridgeReady();
  });

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

  /* Measure the two actual UI histories with public profile transactions.
     The immutable member IDs are captured once from fresh A, and every second
     transaction must rederive the complete vector from that baseline. */
  const baselineSnapshots=themeAxisSnapshots(themeAxisElements());
  const members=baselineSnapshots.map((snapshot,index)=>({snapshot,id:`m${index}`}))
    .filter(({snapshot})=>['small-text','tracking'].some(axis=>
      themeAxisSnapshotEligible(axis,snapshot)));
  const computedRows=()=>members.map(({snapshot,id})=>{
    const style=getComputedStyle(snapshot.element);
    return {id,fontSize:style.fontSize,fontWeight:style.fontWeight,
      letterSpacing:style.letterSpacing};
  });
  const mismatches=(left,right)=>{
    const rightById=new Map(right.map(row=>[row.id,row]));
    return left.filter(row=>{
      const other=rightById.get(row.id);
      return !other||['fontSize','fontWeight','letterSpacing'].some(property=>
        row[property]!==other[property]);
    }).map(row=>row.id);
  };
  const orderResults=[];
  const canonicalRows=new Map();
  for(const small of [.25,.5,.75,1]){
    for(const tracking of [.25,.5,.75,1]){
      await reset();
      const smallFirst=await profile(values(small,null));
      const smallThenTracking=await profile(values(small,tracking));
      const forward=computedRows();
      await reset();
      const trackingFirst=await profile(values(null,tracking));
      const trackingThenSmall=await profile(values(small,tracking));
      const reverse=computedRows();
      const mismatch=mismatches(forward,reverse);
      canonicalRows.set(`${small}/${tracking}`,forward);
      orderResults.push({small,tracking,memberCount:members.length,mismatch,
        transactions:[smallFirst.applied.ok,smallThenTracking.applied.ok,
          trackingFirst.applied.ok,trackingThenSmall.applied.ok],
        requestIdEcho:trackingThenSmall.envelope.requestId===
          trackingThenSmall.data.requestId,
        finalEnvelope:trackingThenSmall.envelope});
    }
  }
  result.profile.order=orderResults;

  /* Negative control: only the second transaction in the tracking-first path
     derives tracking from s0 instead of the final small-text size. The same
     immutable-ID comparator must detect the corrupted implementation. */
  await reset();
  await profile(values(null,.5));
  const originalTrackingSpacing=themeTextProfileTrackingSpacing;
  themeTextProfileTrackingSpacing=(snapshot,_finalSize,value)=>
    originalTrackingSpacing(snapshot,snapshot.fontSize,value);
  let corrupted;
  try{corrupted=await profile(values(.5,.5));}
  finally{themeTextProfileTrackingSpacing=originalTrackingSpacing;}
  result.profile.negativeControl={applied:corrupted.applied,
    mismatch:mismatches(canonicalRows.get('0.5/0.5'),computedRows())};

  /* A profile-aware observer composes both axes for a node added after the
     transaction and extends each independent audit envelope. */
  await reset();
  const dynamicApplied=await profile(values(.5,.5));
  const beforeDynamic=window.AGENTSTACK_THEME_AXIS_STATE();
  const dynamic=document.createElement('div');
  dynamic.textContent='dynamic profile target';
  dynamic.style.cssText='position:fixed;left:20px;top:20px;font-size:10px;'+
    'font-weight:400;letter-spacing:2px;z-index:99999';
  document.body.appendChild(dynamic);await waitFrame();
  const dynamicStyle=getComputedStyle(dynamic);
  const afterDynamic=window.AGENTSTACK_THEME_AXIS_STATE();
  result.profile.dynamic={applied:dynamicApplied.applied,
    computed:{fontSize:dynamicStyle.fontSize,fontWeight:dynamicStyle.fontWeight,
      letterSpacing:dynamicStyle.letterSpacing},before:beforeDynamic,
    after:afterDynamic};
  dynamic.remove();await reset();

  /* Invalid and effect-rejected profiles preserve the complete last-valid
     vector, while response envelopes stay complete and echo requestId. */
  const lastValid=await profile(values(.25,.5));
  const invalidData={type:'agentstack-theme-profile',version:1,
    requestId:'dashboard-profile-invalid',values:values(1.001,.5)};
  const invalidApplied=await applyThemeProfileMessage(invalidData);
  const invalidEnvelope=themeProfileResultMessage(invalidData,invalidApplied);
  const profileHidden=document.createElement('style');
  profileHidden.textContent='html,body,body *{visibility:hidden!important}';
  document.head.appendChild(profileHidden);
  const effectRejected=await profile(values(.75,.75));
  profileHidden.remove();await waitFrame();
  result.profile.rollback={lastValid:lastValid.envelope,invalidEnvelope,
    effectRejected:effectRejected.envelope,
    state:window.AGENTSTACK_THEME_AXIS_STATE()};
  await reset();

  result.final=window.AGENTSTACK_THEME_AXIS_STATE();
  return result;
})()
