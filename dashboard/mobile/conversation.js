/**
 * @author: @kokonutui
 * @description: AI Loading State
 * @version: 2.0.0
 * @date: 2026-10-14
 * @license: MIT
 * @website: https://kokonutui.com
 * @github: https://github.com/kokonut-labs/kokonutui
 *
 * ORRERY: bounded recorded operations, vanilla DOM, no tool execution.
 */
/*
MIT License

Copyright (c) 2025 kokonutUI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/
(function(root){
  'use strict';
  const time=value=>typeof value==='number'&&Number.isFinite(value)?value:
    typeof value==='string'&&value?Date.parse(value):NaN;
  const key=value=>typeof value==='string'&&/^[a-f0-9]{32}$/.test(value);
  const text=value=>typeof value==='string'?value:'';
  function duration(seconds){
    seconds=Math.max(0,Math.floor(seconds));
    const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60),s=seconds%60;
    return (h?h+'h ':'')+(m||h?m+'m ':'')+(m||h?String(s).padStart(2,'0'):s)+'s';
  }
  function elapsed(value){
    if(typeof value!=='string')return null;
    const match=/^\s*(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?\s*$/.exec(value);
    return match&&match.slice(1).some(Boolean)?Number(match[1]||0)*3600+Number(match[2]||0)*60+Number(match[3]||0):null;
  }
  const doingWork=context=>context?.running===true&&context.act_state==='work';
  function collect(events,provider,{working=false,native=true,activeCalls=new Set()}={}){
    const recorded=native&&['dsh','Hermes','Antigravity'].includes(provider);
    const paired=provider==='Claude'||recorded;
    const enabled=recorded||native&&['Claude','Codex'].includes(provider)&&events.some(e=>e.step||e.step_turn||e.step_boundary);
    const pending=new Set();
    if(!enabled&&!working)return {groups:[],consumed:new Set(),pending};
    const groups=[],consumed=new Set(),calls=new Map(),results=new Map(),completions=new Map();
    const codexCalls=new Map(),recordOrder=new Map();
    let current=null,segment='partial',turn=null;
    let tail=[];
    function cut(event){
      if(current)current.closed=true;
      current=null;segment='after:'+event.id;
    }
    function startTurn(event,id,started){
      cut(event);segment='turn:'+event.id;turn={id,started,groups:[]};
    }
    function active(){
      if(!current){
        current={id:segment,steps:new Map(),started:NaN,ended:NaN,duration:null,
          closed:false,anchor:null,orphans:0};
        groups.push(current);turn?.groups.push(current);
      }
      return current;
    }
    for(const [index,event] of events.entries()){
      recordOrder.set(event.id,index);
      const separator=event.step_boundary||event.kind==='text'||event.kind==='mail'||event.mail?.length;
      const boundary=event.step_turn;
      if(working){
        if(!separator&&!boundary&&event.kind==='tool')tail.push(event);
        else tail=[];
      }
      if(!enabled)continue;
      if(provider==='Codex'&&boundary&&key(boundary.id)){
        consumed.add(event.id);
        if(boundary.phase==='start'){
          startTurn(event,boundary.id,boundary.started??event.ts);
        }else if(!turn||turn.id===boundary.id){
          // Native elapsed time describes the whole turn.
          if(turn?.groups.length===1){
            const [only]=turn.groups;
            only.started=time(boundary.started??turn.started);
            only.ended=time(boundary.ended??event.ts);
            if(typeof boundary.duration==='number')only.duration=boundary.duration/1000;
          }
          cut(event);turn=null;
        }
        continue;
      }
      if(provider==='Codex'&&event.step_wrapper===true)consumed.add(event.id);
      const step=event.step;
      if(provider==='Codex'&&event.kind==='tool'&&key(event.tool_call_id)&&!step&&!event.step_wrapper)
        codexCalls.set(event.tool_call_id,event);
      if(step&&key(step.id)){
        if(step.phase==='call'&&paired){
          const owner=active();owner.anchor??=event.id;
          calls.set(step.id,{step,event,owner});
        }else if(step.phase==='result'&&paired){
          consumed.add(event.id);
          results.set(step.id,{step,event,owner:calls.has(step.id)?null:active()});
        }else if(step.phase==='complete'&&provider==='Codex'){
          consumed.add(event.id);
          const request=codexCalls.get(event.tool_call_id||step.id);
          if(request)consumed.add(request.id);
          const id=key(step.turn)?step.turn:turn?.id;
          const completionKey=(id||'')+':'+step.id;
          let completion=completions.get(completionKey);
          if(!completion){
            if(id&&turn?.id!==id)startTurn(request||event,id);
            const owner=active();owner.anchor??=request?.id||event.id;
            completion={owner,eventId:request?.id||event.id,stepId:request?'record:'+request.id:step.id};
            completions.set(completionKey,completion);
          }
          completion.owner.steps.set(completion.stepId,{...step,id:completion.stepId,eventId:completion.eventId});
        }else if(recorded&&step.phase==='complete'){
          const owner=active(),id='record:'+event.id;owner.anchor??=event.id;
          owner.steps.set(id,{...step,id,eventId:event.id});
          consumed.add(event.id);
        }
      }else if(recorded&&event.kind==='tool'&&!event.step_boundary&&!event.step_wrapper){
        const owner=active(),id='record:'+event.id;owner.anchor??=event.id;
        owner.steps.set(id,{id,tool:event.text,status:'observed',started:event.ts,eventId:event.id});
        consumed.add(event.id);
      }else if(recorded&&(event.kind==='tool_result'||event.step_wrapper===true)){
        // An unlinked result has no operation identity or reliable verdict.
        consumed.add(event.id);
      }
      // A tool can also emit a visible Mail card. Assign the step first.
      if(separator)cut(event);
    }
    for(const [id,call] of calls){
      const result=results.get(id);
      if(result){
        consumed.add(call.event.id);
        call.owner.steps.set(id,{...call.step,...result.step,phase:'complete',
          eventId:call.event.id,resultId:result.event.id});
      }else if(recorded||working&&call.owner===current||activeCalls.has(id)){
        // Keep an already displayed request in its original run when an idle
        // observation arrives before the matching result.
        if(!recorded||call.owner===current)pending.add(id);
        consumed.add(call.event.id);
        call.owner.steps.set(id,{...call.step,status:working&&(!recorded||call.owner===current)?'running':'observed',eventId:call.event.id});
      }
    }
    for(const [id,result] of results)if(!calls.has(id)){
      result.owner.orphans++;result.owner.anchor??=result.event.id;
    }
    if(working){
      for(const event of tail){
        if(consumed.has(event.id)||event.step?.phase==='call')continue;
        const owner=active(),id='record:'+event.id;
        consumed.add(event.id);owner.anchor??=event.id;
        owner.steps.set(id,{id,tool:event.text,status:'observed',started:event.ts,eventId:event.id});
      }
    }
    if(enabled&&!current&&(provider==='Claude'||turn))active();
    for(const g of groups){
      g.steps=[...g.steps.values()].sort((a,b)=>recordOrder.get(a.eventId)-recordOrder.get(b.eventId));
      const starts=g.steps.map(s=>time(s.started)).filter(Number.isFinite);
      const ends=g.steps.map(s=>time(s.ended)).filter(Number.isFinite);
      if(!Number.isFinite(g.started)&&starts.length)g.started=Math.min(...starts);
      if(!Number.isFinite(g.ended)&&ends.length)g.ended=Math.max(...ends);
      if(g.duration===null&&Number.isFinite(g.started)&&Number.isFinite(g.ended)&&g.ended>=g.started)
        g.duration=(g.ended-g.started)/1000;
    }
    return {groups,consumed,pending};
  }

  function mount({host,mutate=fn=>fn(),onFocusLost=()=>{},loadDetail,previewImage}){
    const cards=new Map();let model={groups:[],consumed:new Set()},current={},visible=false,timer=0,serial=0,workingModel=false;
    const make=(tag,cls,value)=>{const node=root.document.createElement(tag);node.className=cls;if(value!==undefined)node.textContent=value;return node;};
    function changed(node,value){if(node.textContent!==value)node.textContent=value;}
    function glyph(cls,path){
      const node=make('span',cls);node.setAttribute('aria-hidden','true');
      const svg=root.document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 16 16');
      const shape=root.document.createElementNS('http://www.w3.org/2000/svg','path');shape.setAttribute('d',path);
      svg.append(shape);node.append(svg);return node;
    }
    function icon(status){
      const node=make('span','conversation-step-icon '+status);node.setAttribute('aria-hidden','true');
      const svg=root.document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 16 16');
      const open=status==='observed'||status==='running';
      const shape=root.document.createElementNS('http://www.w3.org/2000/svg',open?'circle':'path');
      if(open){for(const [k,v] of Object.entries({cx:8,cy:8,r:4.5}))shape.setAttribute(k,v);}
      else{shape.setAttribute('d',status==='failed'?'M4.5 4.5 11.5 11.5M11.5 4.5 4.5 11.5':'M3.5 8.5 6.5 11.5 12.5 4.5');
        if(status==='done'){shape.setAttribute('class','conversation-step-check');shape.setAttribute('pathLength','1');}}
      svg.append(shape);node.append(svg);
      return node;
    }
    function setOpen(card,open){
      if(!open&&card.body.contains(root.document.activeElement))card.button.focus({preventScroll:true});
      if(!open)for(const record of card.rows.values())closeDetail(record);
      card.open=open;card.node.classList.toggle('is-open',open);card.button.setAttribute('aria-expanded',String(open));
      card.body.inert=!open;
    }
    function create(id){
      const node=make('section','conversation-steps');node.dataset.stepsGroup=id;node.setAttribute('aria-label','Agent steps');
      const button=make('button','conversation-steps-toggle');button.type='button';
      const symbol=glyph('conversation-steps-symbol','M5 4h8M5 8h8M5 12h8M2.5 4h.01m-.01 4h.01m-.01 4h.01');
      const heading=make('span','conversation-steps-heading'),clock=make('span','conversation-steps-time');
      const summary=make('span','conversation-steps-summary'),count=make('span','conversation-steps-count');
      summary.append(count,clock);button.append(symbol,heading,summary,glyph('conversation-steps-chevron','m4 6 4 4 4-4'));
      const body=make('div','conversation-steps-expansion'),inner=make('div','conversation-steps-inner');
      body.id='conversation-steps-'+(++serial)+'-'+Math.random().toString(36).slice(2);button.setAttribute('aria-controls',body.id);
      const list=make('ol','conversation-steps-list');
      const notice=make('p','conversation-steps-notice','Load earlier to see the matching tool requests.');notice.hidden=true;
      inner.append(list,notice);body.append(inner);node.append(button,body);
      const card={node,button,heading,clock,count,body,list,notice,rows:new Map(),open:false,working:false,waiting:false,
        clockValue:null,clockAt:0,lastWork:null,lastInput:null};
      button.addEventListener('click',()=>mutate(()=>setOpen(card,!card.open),button));
      setOpen(card,false);return card;
    }
    function createRow(animate){
      const node=make('li','conversation-step'+(animate?' is-new':''));
      const copy=make('div','conversation-step-copy'),top=make('div','conversation-step-top');
      const tool=make('span','conversation-step-tool'),outcome=make('span','conversation-step-outcome');
      const clock=make('span','conversation-step-duration'),detail=make('div','conversation-step-target');
      const toggle=make('button','conversation-step-detail-toggle conversation-step-diff-toggle');
      const panel=make('div','conversation-step-detail conversation-step-diff');
      const added=make('span','conversation-diff-added'),removed=make('span','conversation-diff-removed');
      const disclosure=glyph('conversation-step-command-chevron','m4 6 4 4 4-4');
      const exit=make('span','conversation-step-exit');exit.hidden=disclosure.hidden=true;
      toggle.type='button';toggle.hidden=panel.hidden=true;
      panel.id='conversation-step-detail-'+(++serial)+'-'+Math.random().toString(36).slice(2);
      toggle.setAttribute('aria-controls',panel.id);toggle.setAttribute('aria-expanded','false');
      toggle.append(added,removed,disclosure);
      const imageButton=make('button','conversation-step-image');imageButton.type='button';imageButton.hidden=true;
      top.append(tool,toggle,outcome,clock,exit);copy.append(top,detail,panel);node.append(copy,imageButton);
      const record={node,top,tool,outcome,clock,exit,detail,toggle,panel,added,removed,disclosure,
        kind:'',ref:null,imageButton,image:null,request:null,
        icon:null,status:'',fingerprint:''};
      toggle.addEventListener('click',()=>mutate(()=>toggleDetail(record),toggle));
      imageButton.addEventListener('click',()=>{if(record.image)previewImage(record.image);});
      return record;
    }
    function closeDetail(record){
      if(record.panel.hidden)return;
      if(record.panel.contains(root.document.activeElement)&&record.toggle.isConnected)
        record.toggle.focus({preventScroll:true});
      record.request?.abort();record.request=null;
      record.panel.hidden=true;record.panel.replaceChildren();
      record.toggle.setAttribute('aria-expanded','false');
    }
    function showMore(panel,content,count,expand){
      const more=make('button','conversation-step-detail-more','Show '+count+' more lines');
      more.type='button';
      more.addEventListener('click',()=>{expand();panel.tabIndex=-1;panel.focus({preventScroll:true});});
      content.append(more);
      return more;
    }
    function revealDetail(panel,content){
      if(root.OrreryDemo?.on)root.OrreryDemo.maskContent?.(content);
      panel.replaceChildren(content);
    }
    function diffContent(panel,diff,stepPath){
      const total=diff.hunks.reduce((count,hunk)=>count+hunk.lines.length,0);
      const initialLines=8;
      function paintDiff(limit){
        const content=make('div','conversation-step-diff-content');
        const path=diff.path===stepPath?'':diff.path;
        if(diff.new_file||path){
          const caption=make('div','conversation-step-diff-caption',diff.new_file?'New file':path);
          if(diff.new_file&&path)caption.append(make('span','conversation-step-diff-path',' · '+path));
          content.append(caption);
        }
        let remaining=limit;
        for(const hunk of diff.hunks){
          if(remaining<=0)break;
          const group=make('div','conversation-step-diff-hunk');
          if(hunk.header&&(!diff.new_file||diff.hunks.length>1))
            group.append(make('div','conversation-step-diff-header',hunk.header));
          for(const line of hunk.lines.slice(0,remaining)){
            const row=make('div','conversation-step-diff-line');
            row.dataset.op=line.op;
            row.append(make('span','conversation-step-diff-mark',line.op==='-'?'−':line.op),
              make('span','conversation-step-diff-text',line.text));
            group.append(row);remaining--;
          }
          content.append(group);
        }
        if(diff.truncated)content.append(make('p','conversation-step-diff-note','Too long to show here'));
        else if(total>limit)showMore(panel,content,total-limit,()=>paintDiff(total)).classList.add('conversation-step-diff-more');
        revealDetail(panel,content);
      }
      paintDiff(initialLines);
    }
    function commandContent(panel,value){
      const lines=value.output?value.output.replace(/\n$/,'').split('\n'):[];
      function paintCommand(limit){
        const content=make('div','conversation-step-command-content');
        content.append(make('pre','conversation-step-command-code','$ '+value.command));
        if(lines.length)content.append(make('pre','conversation-step-command-output',lines.slice(0,limit).join('\n')));
        else if(!value.truncated)content.append(make('p','conversation-step-detail-note','No output'));
        if(lines.length>limit)showMore(panel,content,lines.length-limit,()=>paintCommand(lines.length));
        if(value.truncated)content.append(make('p','conversation-step-detail-note','Too long to show here'));
        revealDetail(panel,content);
      }
      paintCommand(8);
    }
    async function toggleDetail(record){
      if(!record.panel.hidden){closeDetail(record);return;}
      if(!record.ref)return;
      const request=new AbortController();record.request=request;
      const {panel,toggle,kind}=record;
      const command=kind==='detail';
      panel.hidden=false;toggle.setAttribute('aria-expanded','true');
      const notice=make('p',command?'conversation-step-detail-note':'conversation-step-diff-note',
        command?'Loading command…':'Loading diff…');
      notice.setAttribute('role','status');panel.append(notice);
      try{
        const value=await loadDetail(kind,record.ref,request.signal);
        if(record.request===request){
          if(command)commandContent(panel,value);else diffContent(panel,value,record.detailText);
        }
      }catch(error){
        if(record.request===request)notice.textContent=error.message||(command?'Command unavailable':'Diff unavailable');
      }finally{
        if(record.request===request)record.request=null;
      }
    }
    function detailControl(record,step){
      const stat=step.diff_stat;
      const command=loadDetail&&key(step.detail_ref)&&['Bash','Shell'].includes(step.tool);
      const diff=loadDetail&&key(step.diff_ref)&&stat&&
        Number.isSafeInteger(stat.added)&&stat.added>=0&&Number.isSafeInteger(stat.removed)&&stat.removed>=0;
      const nextKind=command?'detail':diff?'diff':'';
      const nextRef=command?step.detail_ref:diff?step.diff_ref:null;
      if(nextRef!==record.ref||nextKind!==record.kind){
        closeDetail(record);record.ref=nextRef;record.kind=nextKind;record.toggle.hidden=!nextRef;
        if(nextRef)record.toggle.prepend(record.tool);else record.top.prepend(record.tool);
      }
      record.toggle.classList.toggle('conversation-step-diff-toggle',!command);
      record.panel.classList.toggle('conversation-step-diff',!command);
      record.toggle.classList.toggle('conversation-step-command-toggle',Boolean(command));
      if(command)record.toggle.setAttribute('aria-label',step.tool+' command and output');
      else record.toggle.removeAttribute('aria-label');
      record.disclosure.hidden=!command;
      record.added.hidden=!diff||Boolean(command);
      record.removed.hidden=!diff||Boolean(command)||stat.new_file;
      if(!diff||command)return;
      changed(record.added,'+'+stat.added);
      changed(record.removed,stat.new_file?'':'−'+stat.removed);
    }
    function updateRow(record,step){
      const exit=Number.isSafeInteger(step.exit_code)&&step.exit_code!==0?step.exit_code:null;
      const status=exit!==null?'failed':['done','failed','running'].includes(step.status)?step.status:'observed';
      if(status!==record.status){
        const symbol=icon(status);
        if(record.icon)record.icon.replaceWith(symbol);else record.node.prepend(symbol);
        record.icon=symbol;record.status=status;
      }
      changed(record.tool,text(step.tool).split('__').at(-1));
      changed(record.outcome,status==='failed'?(exit===null?'Failed':''):status==='running'?'Running…':status==='observed'?'Observed':'');
      record.exit.hidden=exit===null;changed(record.exit,exit===null?'':'exit '+exit);
      const start=time(step.started),end=time(step.ended);
      changed(record.clock,Number.isFinite(start)&&Number.isFinite(end)&&end>=start?duration((end-start)/1000):'');
      record.detailText=text(step.detail);
      changed(record.detail,record.detailText);record.detail.hidden=!step.detail;
      const image=step.image;
      record.image=previewImage&&step.phase==='complete'&&['done','observed'].includes(status)
        &&image?.kind==='image'&&key(image.ref)&&text(image.label)?image:null;
      record.imageButton.hidden=!record.image;
      record.node.classList.toggle('has-step-image',Boolean(record.image));
      if(record.image)record.imageButton.setAttribute('aria-label','Preview '+image.label);
      detailControl(record,step);
    }
    function title(card,value,working){
      if(card.title===value&&card.wave===working)return;
      card.title=value;card.wave=working;card.heading.replaceChildren();
      if(working){
        Array.from('Working…').forEach((char,index)=>{
          const span=make('span','conversation-steps-letter',char);span.style.animationDelay=(index*.05)+'s';card.heading.append(span);
        });
      }else card.heading.textContent=value;
    }
    function paint(){
      const last=model.groups.at(-1);let ticking=false;
      for(const g of model.groups){
        const card=cards.get(g.id);if(!card)continue;
        const working=g===last&&!g.closed&&current.running===true&&current.act_state==='work';
        const waiting=g===last&&!g.closed&&current.running===true&&['ask','question'].includes(current.act_state);
        const seconds=elapsed(current.work_disp);
        if(working){
          if(card.lastInput!==current.work_disp||!card.working){
            card.clockValue=seconds;card.clockAt=Date.now();card.lastInput=current.work_disp;
          }
          card.lastWork=card.clockValue===null?null:card.clockValue+(Date.now()-card.clockAt)/1000;
        }
        const was=card.working||card.waiting,now=working||waiting;
        if(was&&!now&&card.open)setOpen(card,false);
        card.working=working;card.waiting=waiting;
        const compacting=working&&current.reader_phase==='compacting';
        const finished=g.closed?(g.duration??card.lastWork):(card.lastWork??g.duration);
        const status=working&&!compacting?'Working…':waiting?'Waiting for you':'';
        const unit=now?'step':'tool call';
        const count=g.steps.length+' '+unit+(g.steps.length===1?'':'s');
        const secondsShown=working?(compacting?null:card.lastWork):finished;
        const clock=secondsShown===null?'':', '+duration(secondsShown);
        title(card,status,working&&!compacting);card.heading.hidden=!status;
        changed(card.count,count);changed(card.clock,clock);
        const label=(status?status+' ':'')+count+clock;
        if(card.button.title!==label){card.button.title=label;card.button.setAttribute('aria-label',label);}
        card.node.classList.toggle('is-paused',!visible);
        card.node.hidden=!g.steps.length&&!g.orphans&&(!now||compacting);
        ticking=ticking||working&&visible;
      }
      if(ticking&&!timer)timer=root.setInterval(paint,500);
      if(!ticking&&timer){root.clearInterval(timer);timer=0;}
    }
    function sync({events,provider,mode,rows,context,isVisible}){
      current=context||{};visible=isVisible;
      workingModel=doingWork(current);
      model=collect(events,provider,{working:workingModel,native:mode!=='fresh_tail'&&(mode!=='snapshot'||provider==='Hermes'),activeCalls:model.pending});
      const wanted=new Set(model.groups.map(g=>g.id)),byId=new Map(rows.map(r=>[r.dataset.id,r]));
      for(const [id,card] of cards)if(!wanted.has(id)){
        if(card.node.contains(root.document.activeElement))onFocusLost();
        for(const record of card.rows.values())closeDetail(record);
        card.node.remove();cards.delete(id);
      }
      for(const g of model.groups){
        const existed=cards.has(g.id),card=cards.get(g.id)||create(g.id);cards.set(g.id,card);
        const ids=new Set(g.steps.map(s=>s.id));
        for(const [id,record] of card.rows)if(!ids.has(id)){
          if(record.node.contains(root.document.activeElement))card.button.focus({preventScroll:true});
          closeDetail(record);record.node.remove();card.rows.delete(id);
        }
        for(const [index,step] of g.steps.entries()){
          const fingerprint=JSON.stringify(step),record=card.rows.get(step.id)||createRow(existed);
          if(record.fingerprint!==fingerprint){updateRow(record,step);record.fingerprint=fingerprint;}
          card.rows.set(step.id,record);
          const node=record.node;
          if(card.list.children[index]!==node)card.list.insertBefore(node,card.list.children[index]||null);
        }
        card.notice.hidden=!g.orphans;
        const anchor=byId.get(g.anchor);
        if(anchor){if(card.node.nextSibling!==anchor)anchor.before(card.node);}
        else if(card.node.parentElement!==host)host.append(card.node);
      }
      paint();return model.consumed;
    }
    function closeDetails(){for(const card of cards.values())for(const record of card.rows.values())closeDetail(record);}
    return Object.freeze({sync,closeDetails,needsSync:context=>workingModel!==doingWork(context),
      activity(context,isVisible){current=context||{};visible=isVisible;paint();return [...cards.values()].some(c=>!c.node.hidden&&(c.working||c.waiting));},
      reset(){closeDetails();if(timer)root.clearInterval(timer);timer=0;for(const card of cards.values())card.node.remove();cards.clear();model={groups:[],consumed:new Set()};workingModel=false;},
    });
  }
  const api={collect,duration,elapsed,mount};
  root.OrreryMobileSteps=Object.freeze(api);
  if(typeof module!=='undefined')module.exports=api;
})(typeof window==='undefined'?globalThis:window);

/* Opt-in, read-only conversation history. File previews show plain text,
   inert Markdown from the bundled renderer, or a PNG the relay checked.
   Save downloads the previewed content, including local Markdown source edits,
   through one object URL revoked on close. Nothing is kept in storage or URLs; unmount clears the
   DOM, the preview, requests, timers and pending rows. */
(()=>{'use strict';
const make=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!==undefined)e.textContent=txt;return e;};
const MAX_BYTES=768*1024,MAX_KEPT=200,POLL=5000;
const MAX_PREVIEW_BYTES=6*1024*1024,MAX_SIDE=8192,MAX_PIXELS=8000000,REF=/^[a-f0-9]{32}$/,BASE64=/^(?:[A-Za-z0-9+/]{4})+(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
let capability=null,gen=0,timer=0,name='',root=null,list=null,status=null,olderButton=null,retryButton=null;
/* Longer than the relay's 15-second wait for cockpit, so its own answer arrives first. */
const WAIT=20000;
/* Poll and Show older each own a controller, so neither aborts the other. */
const controllers={poll:null,older:null,preview:null};
let panel=null,saveUrl='',previewFocus=null,previewRequest=null,previewRetry=false;
let events=[],seen=new Set(),older=null,pending=[],failures=0,olderLoad=null,following=true,savedTop=0;
const pendingRows=new WeakMap(),matchedSends=new Set();
const replyMotion=()=>window.OrreryReaderReplyMotion;
let lastRecordPoll=null;
let recordVersion=0,recordConfirmed=false,replySelection=null;
const referenceCards=new WeakMap();
const referenceClock=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Tokyo',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
const missingReference='The quoted message is not in the loaded part of this conversation.';
let referenceSerial=0,referenceJump=null,referenceFlash=null,referenceTimer=0;
function dismissReferenceSelection(){replySelection?.remove();replySelection=null;}
function selectionInRecord(){const selection=window.getSelection?.();return !!(selection?.toString()&&list?.contains(selection.anchorNode));}
function selectionReply(){
 const selection=window.getSelection?.(),terminal=window.OrreryMobileTerminal;
 if(!root?.isConnected||!recordConfirmed||!terminal?.canReference?.()||!selection?.rangeCount||!selection.toString().trim()){dismissReferenceSelection();return;}
 const range=selection.getRangeAt(0),parent=node=>node?.nodeType===1?node:node?.parentElement;
 const start=parent(range.startContainer)?.closest('.conversation-text'),end=parent(range.endContainer)?.closest('.conversation-text');
 if(!start||start!==end||!list.contains(start)){dismissReferenceSelection();return;}
 if([...start.querySelectorAll('button,.conversation-code-head')].some(node=>range.intersectsNode(node))){dismissReferenceSelection();return;}
 const row=start.closest('[data-id]'),event=events.find(e=>e.id===row?.dataset.id);
 if(!event||event.kind!=='text'||!['user','assistant'].includes(event.role)){dismissReferenceSelection();return;}
 const box=range.getBoundingClientRect(),viewport=window.visualViewport;
 const top=box.bottom+10,bottom=(viewport?.offsetTop||0)+(viewport?.height||innerHeight);
 if(box.bottom<=list.getBoundingClientRect().top||top+44>bottom-8){dismissReferenceSelection();return;}
 const heldRoot=root,version=recordVersion,agent=name;
 const value={agent,role:event.role,ts:event.ts,eventId:event.id,text:selection.toString(),
  isCurrent:()=>root===heldRoot&&recordConfirmed&&recordVersion===version&&events.includes(event)};
 const button=replySelection||make('button','reader-selection-reference','Reply with reference');button.type='button';
 button.style.left=Math.max(12,Math.min(box.left,innerWidth-238))+'px';button.style.top=top+'px';
 button.onpointerdown=event=>event.preventDefault();button.onmousedown=event=>event.preventDefault();
 button.onclick=()=>terminal.reference(value,()=>{
  selection.removeAllRanges();dismissReferenceSelection();
 });
 if(!replySelection)document.querySelector('#terminal-sheet').append(button);replySelection=button;
}
document.addEventListener('selectionchange',selectionReply);
window.addEventListener('resize',selectionReply);
window.visualViewport?.addEventListener('resize',selectionReply);
let mailResize=null,mailWidth=0;
let toolGroups=[],toolGroupFor=new Map(),toolSerial=0;
let stepView=null,provider='';
function stepContext(a=window.OrreryMobileAgent?.forName(name)){
 const running=a?.running===true&&!a.stale;
 const compacting=running&&a.readerPhase==='compacting';
 return {running,act_state:a?.state,work_disp:a?.workDisplay,
  reader_phase:compacting?'compacting':'',reader_phase_elapsed:compacting?a.readerPhaseElapsed:''};
}
function stepsVisible(){return !!root?.isConnected&&!document.hidden;}
const thumbnails=new Map();let thumbnailObserver=null;
const messages={
 history_unconfirmed:'This agent’s record is not verified as its own, so history stays hidden.',
 history_stale:'The verified record predates this launch, so history stays hidden.',
 agent_unavailable:'This agent is not in the current record list.',
 provider_unavailable:'History is not available for this kind of agent.',
 dashboard_upgrade_required:'Update Telemetry to read this agent’s history.',
 claude_next_phase:'History is not available for this agent yet.',
 invalid_binding:'The record could not be verified.',
 history_unreadable:'The record could not be read.',
 record_too_large:'The latest record is too large to show.',
 page_expired:'Older page expired. Showing the latest page.',
 history_slow:'Taking longer than usual: the computer is busy. Retrying, or tap Retry.',
 older_slow:'Older messages are taking longer than usual. Tap Show older again.',
 omitted:'Some messages are omitted here: too long to show or not loadable.',
 kept_limit:'Showing the latest 200 messages. Older messages are not loaded.',
};
const previewMessages={
 preview_expired:'This preview link expired. Reopen the conversation and tap the file again.',
 preview_too_large:'Too large to preview on the phone.',
 preview_type_denied:'This kind of file cannot be previewed.',
 preview_path_denied:'Not previewable: outside the agent’s folders.',
 preview_permission_denied:'OS permissions do not allow reading this file.',
 preview_changed:'The file changed while it was read. Tap again.',
 history_slow:'Taking longer than usual: the computer is busy. Tap the file again.',
};
const diffMessages={
 diff_expired:'Diff expired. Reopen the conversation and try again.',
 diff_too_large:'Too long to show here',
 diff_changed:'The record changed. Reopen the conversation.',
 history_slow:'Taking longer than usual. Tap again to retry.',
};
const commandMessages={
 detail_expired:'Command link expired. Reopen the conversation and try again.',
 detail_too_large:'Too long to show here',
 detail_changed:diffMessages.diff_changed,
 history_slow:diffMessages.history_slow,
};
async function enabled(){
 if(capability!==null)return capability;
 try{
  const r=await fetch('/view/history',{credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer'});
  const v=r.ok?await r.json():null;capability=!!(v&&v.enabled===true);
 }catch(_){capability=false;}
 return capability;
}
async function request(token,slot){
 controllers[slot]?.abort();const c=new AbortController();controllers[slot]=c;let late=false;const t=setTimeout(()=>{late=true;c.abort();},WAIT);
 try{
  const r=await fetch('/view/conversation?name='+encodeURIComponent(name)+(token?'&page='+token:''),
   {credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',signal:c.signal});
  const raw=await r.text();if(new TextEncoder().encode(raw).length>MAX_BYTES)throw Error('unavailable');
  const v=JSON.parse(raw);if(r.status===410)return {ok:false,error:'page_expired'};
  if(!v||typeof v!=='object')throw Error('unavailable');
  if(v.ok===true&&(v.agent!==name||!Array.isArray(v.events)||v.events.length>40))throw Error('unavailable');
  return v;
 }catch(e){
  // A request replaced by a newer one of the same kind is not a failure;
  // the newer one owns the retry timer.
  if(c.signal.aborted&&!late)e.superseded=true;if(late)e.slow=true;throw e;
 }finally{clearTimeout(t);if(controllers[slot]===c)controllers[slot]=null;}
}
async function loadStepDetail(kind,ref,signal){
 const command=kind==='detail',unavailable=command?'Command unavailable':'Diff unavailable';
 if(!recordConfirmed||!REF.test(ref))throw Error(unavailable);
 const controller=new AbortController(),abort=()=>controller.abort();
 signal.addEventListener('abort',abort,{once:true});
 const timer=setTimeout(abort,WAIT);
 try{
  const route=command?'/view/conversation/detail':'/view/conversation/diff';
  const messages=command?commandMessages:diffMessages;
  const response=await fetch(route+'?name='+encodeURIComponent(name)+'&ref='+ref,
   {credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',signal:controller.signal});
  const raw=await response.text();
  const maxBytes=command?(8+64)*1024*6+1024:64*1024;
  if(new TextEncoder().encode(raw).length>maxBytes)throw Error(messages[kind+'_too_large']);
  const value=JSON.parse(raw);
  if(!response.ok||value?.ok!==true){
   const code=value?.error;
   throw Error(Object.hasOwn(messages,code)?messages[code]:unavailable);
  }
  const valid=command?value.kind==='command'&&typeof value.command==='string'&&typeof value.output==='string':
   typeof value.path==='string'&&typeof value.new_file==='boolean'&&Array.isArray(value.hunks);
  if(!valid||typeof value.truncated!=='boolean')throw Error(unavailable);
  return value;
 }finally{clearTimeout(timer);signal.removeEventListener('abort',abort);}
}
function clock(ts){const d=new Date(ts);return Number.isFinite(d.getTime())?d.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}):'';}
const markdown=()=>window.OrreryConversationMarkdown;
function paintSpeaker(head){
 const recorded=head.dataset.model||'',profile=window.OrreryMobileProfile?.forName(name,recorded),model=recorded||profile?.model||'';
 const key=JSON.stringify([name,model,profile?.provider,profile?.locality]);if(head.dataset.profile===key)return;head.dataset.profile=key;
 const face=window.OrreryMobileProfile?.face(name,recorded);head.querySelector('.conversation-avatar')?.remove();
 if(face){face.classList.add('conversation-avatar');head.prepend(face);}
 head.querySelector('.conversation-speaker-name').textContent=name;
}
function speaker(e){
 const head=make('div','conversation-byline');head.dataset.model=typeof e.model==='string'?e.model:'';
 head.append(make('span','conversation-speaker-name'));
 const at=clock(e.ts);if(at){const time=make('time','conversation-speaker-time',at);time.dateTime=e.ts;head.append(time);}
 paintSpeaker(head);return head;
}
function refreshProfiles(){
 if(!list)return;
 let mailChanged=false;
 list.querySelectorAll('.conversation-byline').forEach(paintSpeaker);
 for(const mark of list.querySelectorAll('.conversation-mail-mark')){
  const peer=mark.closest?.('.conversation-mail-peer');if(peer)peer.disabled=!window.OrreryMobileProfile?.forName(mark.dataset.peerName);
  const p=window.OrreryMobileProfile?.forName(mark.dataset.peerName),key=JSON.stringify(p)||'';if(mark.dataset.profile===key)continue;mark.dataset.profile=key;mailChanged=true;
  const face=p&&window.OrreryMobileProfile?.face(mark.dataset.peerName);mark.replaceChildren();mark.hidden=!face;if(face){face.classList.add('conversation-avatar');mark.append(face);}
 }
 if(mailChanged)layoutMailRoutes();
}
function peerWidth(peer){
 const mark=peer.querySelector('.conversation-mail-mark'),label=peer.querySelector('.conversation-mail-peer-name');
 return label.scrollWidth+(mark.hidden?0:mark.getBoundingClientRect().width+5);
}
function layoutMailThread(card){
 const line=card.querySelector('.conversation-mail-thread'),avatar=card.querySelector('.conversation-mail-sender .conversation-avatar');
 if(card.getAttribute('aria-expanded')!=='true'||!avatar){
  if(!line.hidden)line.hidden=true;
  if(card.classList.contains('is-threaded'))card.classList.remove('is-threaded');
  return;
 }
 // Keep the body's inset while measuring: removing it can clamp scrollTop.
 const box=card.getBoundingClientRect(),face=avatar.getBoundingClientRect();
 if(!face.width)return;
 const x=face.left+face.width/2-box.left,inset=(x+16)+'px',left=(x-.5)+'px',top=(face.bottom-box.top+3)+'px';
 if(card.style.getPropertyValue('--mail-thread-inset')!==inset)card.style.setProperty('--mail-thread-inset',inset);
 if(line.style.left!==left)line.style.left=left;
 if(line.style.top!==top)line.style.top=top;
 if(!card.classList.contains('is-threaded'))card.classList.add('is-threaded');
 if(line.hidden)line.hidden=false;
}
function layoutMailRoutes(){
 if(!list?.isConnected)return;
 for(const route of list.querySelectorAll('.conversation-mail-route')){
  const recipients=route.querySelector('.conversation-mail-recipients'),peers=[...recipients.querySelectorAll('.conversation-mail-peer')],more=recipients.querySelector('.conversation-mail-more');
  recipients.dataset.mode=peers.length===1?'single':'names';peers.forEach(p=>p.hidden=false);more.hidden=true;more.style.marginLeft='0';
  if(peers.length<=1)continue;
  const sender=route.querySelector('.conversation-mail-sender'),fixed=peerWidth(sender)+route.querySelector('.conversation-mail-chevron').getBoundingClientRect().width+route.querySelector('.conversation-mail-arrow').getBoundingClientRect().width+18;
  const available=route.clientWidth-fixed,needed=peers.reduce((sum,p)=>sum+peerWidth(p),0)+(peers.length-1)*6;
  if(needed<=available)continue;
  recipients.dataset.mode='icons';
  const known=peers.filter(p=>!p.querySelector('.conversation-mail-mark').hidden),budget=Math.max(0,available);
  let visible=known.length;
  // Keep the sender's full name before spending width on more recipient icons.
  for(;;){
   const omitted=peers.length-visible;more.textContent=omitted?'+'+omitted:'';more.hidden=!omitted;
   const width=(visible?18+(visible-1)*14:0)+(omitted?(visible?6:0)+more.scrollWidth:0);
   if(width<=budget||visible===0)break;visible--;
  }
  const shown=new Set(known.slice(0,visible));peers.forEach(p=>p.hidden=!shown.has(p));
  more.style.marginLeft=visible?'6px':'0';
 }
 for(const card of list.querySelectorAll('.conversation-mail'))layoutMailThread(card);
}
function body(text,assistant=false,images){
 const box=make('div','conversation-text'),m=markdown();
 if(m&&assistant){
  m.renderDocument(text,box,{codeControls:true});
  renderMarkdownImages(box,images,m,{barePaths:true});
 }
 else if(m)m.renderText(text,box);else box.textContent=text;
 return box;
}
function replyReference(text,withAttachments=false){
 if(typeof text!=='string'||!text.startsWith('Reference: '))return null;
 const lines=text.replace(/\r\n?/g,'\n').split('\n');
 // The mobile prepare/confirm contract carries a single line with a JSON string for the quote.
 const oneLine=lines.length===1&&/^Reference: (.+?) \| Recorded message: (\S+) \| Quote: ("(?:[^"\\\x00-\x1f]|\\.)*") \| Reply:(?: (.*))?$/.exec(lines[0]);
 const fields=(oneLine?oneLine[1]:lines[0].slice(11)).split(' · '),[agent,role,ts]=fields;
 const id=oneLine?oneLine[2]:/^Recorded message: (\S+)$/.exec(lines[1]||'')?.[1];
 if(!id||!agent||agent.trim()!==agent||!/^\S+$/.test(role||'')||fields.length<2||fields.length>3)return null;
 if(ts!==undefined&&(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(ts)||!Number.isFinite(Date.parse(ts))))return null;
 let quote,reply;
 if(oneLine){
  try{quote=JSON.parse(oneLine[3]);}catch(_){return null;}
  reply=oneLine[4]??'';
 }else{
  let end=2;
  while(lines[end]?.startsWith('> '))end++;
  if(end===2||lines[end]!=='')return null;
  quote=lines.slice(2,end).map(line=>line.slice(2)).join('\n');
  reply=lines.slice(end+1).join('\n');
 }
 return quote.trim()&&(reply.trim()||oneLine&&withAttachments)?{agent,ts,id,quote,reply}:null;
}
function referenceRow(id){
 const target=list?.querySelector('[data-id="'+CSS.escape(id)+'"]');
 return target&&(!target.closest('[hidden]')||toolGroupFor.has(id))?target:null;
}
function updateReferenceCards(){
 for(const card of list.querySelectorAll('.conversation-reply-reference')){
  const ref=referenceCards.get(card);
  const available=recordConfirmed&&(referenceRow(ref.id)||older&&events.length<MAX_KEPT&&older!==ref.failedCursor);
  card.setAttribute('aria-disabled',String(!available));
  card.title=ref.label+'\n'+ref.quote+(available?'':'\n\n'+missingReference);
  card.setAttribute('aria-label',available?'Jump to '+ref.label:ref.label+'. '+missingReference);
 }
}
function clearReferenceFlash(){
 clearTimeout(referenceTimer);referenceFlash?.classList.remove('conversation-reference-target');referenceFlash=null;
}
async function jumpReference(card,ref){
 if(referenceJump||card.getAttribute('aria-disabled')==='true')return;
 const g=gen,version=recordVersion,cursors=new Set();
 const current=()=>g===gen&&version===recordVersion&&recordConfirmed&&list?.isConnected&&list.contains(card);
 referenceJump=card;card.setAttribute('aria-busy','true');
 try{
  let target=referenceRow(ref.id);
  while(!target&&older&&events.length<MAX_KEPT&&!cursors.has(older)&&cursors.size<MAX_KEPT){
   cursors.add(older);
   const loaded=await loadOlder();
   if(!current())return;
   target=referenceRow(ref.id);
   if(!loaded)break;
  }
  if(!current())return;
  if(!target){ref.failedCursor=older;return;}
  const group=toolGroupFor.get(ref.id);if(group&&!group.open)setToolGroupOpen(group,true);
  const inset=parseFloat(getComputedStyle(list).scrollPaddingTop)||0;
  list.scrollTop+=target.getBoundingClientRect().top-list.getBoundingClientRect().top-inset;
  following=atBottom();savedTop=list.scrollTop;
  target.tabIndex=-1;target.focus({preventScroll:true});
  clearReferenceFlash();referenceFlash=target;target.classList.add('conversation-reference-target');
  referenceTimer=setTimeout(clearReferenceFlash,1550);
 }finally{
  if(referenceJump===card)referenceJump=null;
  card.removeAttribute('aria-busy');if(current())updateReferenceCards();
 }
}
function referenceCard(ref){
 const card=make('button','conversation-reply-reference');
 card.type='button';
 const at=ref.ts?referenceClock.format(Date.parse(ref.ts)):'';
 const portrait=/^[A-Za-z0-9_-]{1,64}$/.test(ref.agent)&&window.OrreryMobileProfile?.face(ref.agent);
 const avatar=portrait||make('span','face');
 if(!portrait)avatar.append(make('span','initials',Array.from(ref.agent)[0]));
 avatar.classList.add('conversation-avatar');avatar.setAttribute('aria-hidden','true');
 const name=make('span','conversation-reply-reference-agent',ref.agent);
 const quote=make('span','conversation-reply-reference-quote',ref.quote.trimStart().split('\n',1)[0].trimEnd());
 quote.id='reader-reply-quote-'+(++referenceSerial);card.setAttribute('aria-describedby',quote.id);
 const arrow=make('span','conversation-reply-reference-icon','↗');arrow.setAttribute('aria-hidden','true');
 ref.label='quoted message from '+ref.agent+(at?' at '+at+' JST':'');
 referenceCards.set(card,ref);card.append(avatar,name,quote,arrow);card.onclick=()=>jumpReference(card,ref);
 return card;
}
function mailCard(m,ts,images){
 const card=make('div','conversation-mail '+m.direction);
 const head=make('div','conversation-mail-head'),route=make('div','conversation-mail-route');
 const peer=(value,strong)=>{const p=make('button','conversation-mail-peer'+(strong?' is-counterpart':'')),mark=make('span','conversation-mail-mark');p.type='button';mark.dataset.peerName=value||'';mark.hidden=true;p.title=value||'unknown';p.setAttribute('aria-label','Open '+(value||'unknown')+' conversation');p.disabled=!window.OrreryMobileProfile?.forName(value);p.onclick=e=>{e.stopPropagation();if(value&&value!==name)window.OrreryMobileTerminal?.open(value,window.OrreryMobileIdentity?.forName(value),'conversation');};p.append(mark,make('span','conversation-mail-peer-name',value||'unknown'));return p;};
 const sender=peer(m.sender||(m.direction==='sent'?name:'recorded sender unavailable'),m.direction==='received');sender.classList.add('conversation-mail-sender');
 const targets=m.recipients.length?m.recipients:[m.direction==='received'?name:'recorded recipient unavailable'];
 const recipients=make('span','conversation-mail-recipients');recipients.title=targets.join(', ');recipients.setAttribute('role','group');recipients.setAttribute('aria-label','Recipients: '+targets.join(', '));
 for(const recipient of targets)recipients.append(peer(recipient,m.direction==='sent'));
 const more=make('span','conversation-mail-more');more.hidden=true;recipients.append(more);
 route.append(make('span','conversation-mail-chevron','▸'),sender,make('span','conversation-mail-arrow','→'),recipients);
 head.append(route);const at=clock(ts);if(at){const time=make('time','conversation-mail-time',at);time.dateTime=ts;head.append(time);}
 const subject=make('span','conversation-mail-subject-row');
 if(['high','urgent'].includes(m.importance)){const dot=make('span','conversation-mail-importance');dot.dataset.importance=m.importance;dot.title=dot.ariaLabel=m.importance==='high'?'High importance':'Urgent importance';dot.setAttribute('role','img');subject.append(dot);}
 subject.append(make('strong','conversation-mail-subject',m.subject||'(no subject)'));
 card.append(make('div','conversation-mail-label','Mail · '+(m.direction==='received'?'Received':'Sent')),head);
 const detail=make('div','conversation-mail-body');detail.hidden=true;
 const note=make('small','conversation-mail-note',m.coverage==='preview'?'Recorded preview only':m.coverage==='unavailable'?'Body not in the record':m.truncated?'Shortened':'Tap to show the recorded body');
 const line=make('span','conversation-mail-thread');line.hidden=true;line.setAttribute('aria-hidden','true');
 const toggle=make('button','conversation-mail-toggle');toggle.type='button';toggle.append(subject,note);toggle.setAttribute('aria-expanded','false');
 card.append(toggle,detail,line);card.setAttribute('aria-expanded','false');
 const closedNote=note.textContent;
 card.onclick=event=>{
  if(detail.contains(event.target))return;
  if(detail.hidden&&!detail.hasChildNodes())detail.append(body(m.body,true,images));
  detail.hidden=!detail.hidden;
  if(!detail.hidden)loadThumbnails();
  card.setAttribute('aria-expanded',String(!detail.hidden));
  toggle.setAttribute('aria-expanded',String(!detail.hidden));
  card.querySelector('.conversation-mail-chevron').textContent=detail.hidden?'▸':'▾';
  note.textContent=detail.hidden?closedNote:'Recorded body · tap to hide';
  layoutMailThread(card);
 };
 return card;
}
// The cockpit puts file references on the file tool calls (Read, Write,
// Edit, apply_patch, SendUserFile...), so a tool row carries chips too.
function files(e,r){if(Array.isArray(e.files)&&e.files.length){const f=make('div','conversation-files');e.files.forEach(x=>f.append(chip(x)));r.append(f);}}
const terminalCSI=/(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]/g;
function localCommand(e){
 const text=e.input_display_text??e.text,command=markdown()?.commandRecord(text);
 const localOutput=command&&!command.shell&&command.outputs.length>0;
 const output=localOutput?text.replace(terminalCSI,''):text;
 const compact=provider==='Claude'&&!e.truncated&&command&&(
  command.command==='/compact'||command.command?.startsWith('/compact ')||
  command.outputs.length===1&&command.outputs[0].label==='output'&&
   command.outputs[0].text.replace(terminalCSI,'').trimStart().split(/\r?\n/,1)[0].trimEnd()==='Compacted (ctrl+o to see full summary)');
 return {command,output,compact};
}
const compactRecord=e=>e?.kind==='tool'&&e.step_boundary===true&&e.text==='Context compacted';
function row(e){
 if(e.kind==='tool'){const r=make('div','conversation-tool');r.append(make('span','conversation-tool-label',e.text));r.dataset.id=e.id;files(e,r);if(Array.isArray(e.mail))e.mail.forEach(m=>r.append(mailCard(m,e.ts,e.markdown_images)));return r;}
 if(e.kind==='mail'){const r=make('div','conversation-mails');r.dataset.id=e.id;e.mail.forEach(m=>r.append(mailCard(m,e.ts,e.markdown_images)));return r;}
 const record=e.role==='user'?localCommand(e):null,command=record?.command;
 if(record?.compact){
  const r=make('div','conversation-compact-envelope');r.dataset.id=e.id;r.hidden=true;return r;
 }
 const visibleText=e.input_display_text??e.text;
 const human=e.kind==='text'&&e.role==='user'&&!command;
 const r=make('div',command?.shell?'conversation-shell':'conversation-bubble '+(e.role==='user'?'user':'assistant'));r.dataset.id=e.id;
 const time=clock(e.ts);
 if(human)r.title=time?'You · '+time:'You';
 if(command){if(command.shell)r.append(make('small','conversation-speaker','SHELL'));markdown().renderCommand(record.output,r);}
 else{
  const reference=human&&!e.truncated&&replyReference(visibleText,e.input_display_text!==undefined&&Boolean(e.files?.length));
  if(e.role==='assistant')r.append(speaker(e));
  const text=body(reference?reference.reply:visibleText,e.role==='assistant',e.markdown_images);
  if(reference){
   r.classList.add('conversation-has-reference');
   const wash=make('div','conversation-reference-body');wash.append(text);
   r.append(referenceCard(reference),wash);
  }else r.append(text);
 }
 if(command||e.role==='assistant')window.OrreryReaderReplyMotion?.enhanceCopies?.(r);

 if(Array.isArray(e.mail))e.mail.forEach(m=>r.append(mailCard(m,e.ts,e.markdown_images)));
 files(e,r);
 const meta=[human||e.role==='assistant'&&!command?'':time,e.truncated?'shortened':''].filter(Boolean).join(' · ');if(meta)r.append(make('small','conversation-time',meta));
 return r;
}
function chip(x){
 const label=(x.kind==='image'?'Image · ':'File · ')+x.label;
 if(typeof x.ref!=='string'||!REF.test(x.ref))return make('span','conversation-chip '+x.kind,label);
 const b=make('button','conversation-chip '+x.kind+' previewable',label);b.type='button';
 b.setAttribute('aria-label','Preview '+x.label);b.onclick=()=>openPreview(x);return b;
}
/* 表示済み画像は tool を閉じても会話に残す。人の入力画像は吹き出しと
   同じ側に置き、参照と画像は既存の preview の許可を使う。 */
function shownFiles(e){
 if(e.shown!==true||!Array.isArray(e.files)||!e.files.length)return null;
 const box=make('div','conversation-shown-files');box.dataset.shownFor=e.id;
 box.dataset.role=e.role==='user'?'user':'assistant';
 for(const x of e.files){
  if(x.kind!=='image'||typeof x.ref!=='string'||!REF.test(x.ref)){
   if(e.role!=='user')box.append(make('span','conversation-chip',x.label));
   continue;
  }
  box.append(thumbnail(x));
 }
 return box.childElementCount?box:null;
}
function thumbnail(x,alt=x.label,fallback=alt){
 const button=make('button','conversation-thumbnail'+(x.markdown?' conversation-markdown-image':''));
 button.type='button';button.setAttribute('aria-label',(x.markdown?'Open image: ':'画像を拡大: ')+alt);
 if(x.markdown)button.title='Open image';
 const image=make('img','conversation-thumbnail-image');image.alt=alt;image.hidden=true;
 const label=make('span','conversation-thumbnail-label',fallback);
 const hint=make('small','conversation-thumbnail-status',x.markdown?'Loading image…':'画像を読み込み中…');
 button.append(image,label,hint);button.onclick=()=>openPreview(x);
 const item={button,image,label,hint,x,alt,fallback,state:'waiting',visible:typeof IntersectionObserver!=='function',observed:false,controller:null};
 thumbnails.set(button,item);
 return button;
}
function observeThumbnail(button){
 if(typeof IntersectionObserver==='function'){
  if(!thumbnailObserver)thumbnailObserver=new IntersectionObserver(entries=>{
   for(const entry of entries){const item=thumbnails.get(entry.target);if(item)item.visible=entry.isIntersecting;}
   loadThumbnails();
  },{root:list,rootMargin:'160px'});
  thumbnailObserver.observe(button);
 }
}
function renderMarkdownImages(host,images,renderer,{barePaths=false}={}){
 const refs=new Map();
 if(Array.isArray(images))for(const item of images.slice(0,8)){
  if(typeof item?.target==='string'&&typeof item.ref==='string'&&REF.test(item.ref))refs.set(item.target,item.ref);
 }
 // The bundled renderer already recognizes images and keeps raw HTML literal.
 // Only the Reader can resolve its image markers against this event's IDs.
 for(const marker of host.querySelectorAll('span.markdown-image[title]')){
  if(marker.parentElement.closest('a,[data-path-label],.markdown-link-text'))continue;
  const target=marker.title,text=marker.textContent;
  const alt=text.startsWith('[image: ')?text.slice(8,-1):'';
  const caption=alt?alt+' ('+target+')':target;
  if(/^https?:\/\//i.test(target)){
   const destination=/[<>]/.test(target)?target:'<'+target+'>';
   const fragment=document.createDocumentFragment();
   renderer.renderDocument('[Image]('+destination+')',fragment);
   const link=fragment.querySelector('a');
   if(!link)continue;
   link.className='markdown-image';link.textContent=caption;
   marker.replaceWith(link);continue;
  }
  if(!/^(?:\/(?!\/)|~\/)[^\x00-\x1f\x7f<>]*\.(?:png|jpe?g|gif|webp)$/i.test(target))continue;
  const ref=refs.get(target),label=target.split('/').pop();
  if(ref){
   marker.replaceWith(thumbnail({ref,label,markdown:true},alt||label,caption));
  }else marker.textContent=caption;
 }
 if(!barePaths||!refs.size)return;
 // Use the same path candidates as the desktop. Match a whole line of
 // assistant prose, even when Markdown combines it with adjacent lines.
 for(const marker of host.querySelectorAll(':scope > p > span[data-path-candidate]')){
  const target=marker.dataset.pathCandidate,ref=refs.get(target);
  if(!ref||marker.dataset.pathLabel||!target.startsWith('/')||target.startsWith('//'))continue;
  let before=marker.previousSibling,after=marker.nextSibling;
  while(before&&/^[ \t]*$/.test(before.textContent))before=before.previousSibling;
  while(after&&/^[ \t]*$/.test(after.textContent))after=after.nextSibling;
  if((before&&!/\n[ \t]*$/.test(before.textContent))||(after&&!/^[ \t]*\n/.test(after.textContent)))continue;
  const label=target.split('/').pop();
  const button=make('button','conversation-path-preview',target);
  button.type='button';button.setAttribute('aria-label','Preview image: '+label);
  button.onclick=()=>openPreview({ref,label,markdown:true});
  marker.replaceWith(button);
 }
}
function forgetThumbnails(node){
 for(const [button,item] of thumbnails)if(!node||node.contains(button)){
  item.controller?.abort();thumbnailObserver?.unobserve(button);clearImage(item.image);thumbnails.delete(button);
 }
 if(!node){thumbnailObserver?.disconnect();thumbnailObserver=null;}
}
function clearImage(img){img.src='';}
function paintImage(img,v){img.src=pngSource(v);}
function thumbnailFailed(item){
 if(!item.image.hidden){clearImage(item.image);item.image.hidden=true;}
 item.label.textContent=item.fallback;item.hint.hidden=false;
 item.hint.textContent=item.x.markdown?'Image unavailable':'画像を取得できません。タップして開く';
 item.state='failed';
}
function loadThumbnails(){
 if(!root||document.hidden)return;
 let active=[...thumbnails.values()].filter(x=>x.state==='loading').length;
 for(const item of thumbnails.values()){
  if(!item.button.isConnected)continue;
  if(!item.observed){item.observed=true;observeThumbnail(item.button);}
  if(active>=2||item.state!=='waiting'||!item.visible)continue;
  active++;loadThumbnail(item);
 }
}
async function loadThumbnail(item){
 const g=gen,c=new AbortController();item.controller=c;item.state='loading';
 const t=setTimeout(()=>c.abort(),WAIT);let value=null;
 try{
  const response=await fetch('/view/conversation/preview?name='+encodeURIComponent(name)+'&ref='+item.x.ref+'&thumbnail=1',
   {credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',signal:c.signal});
  const raw=await response.text();if(raw.length>MAX_PREVIEW_BYTES)throw Error('unavailable');
  if(response.ok)value=JSON.parse(raw);
 }catch(_){}finally{clearTimeout(t);item.controller=null;}
 if(!thumbnails.has(item.button)||item.state!=='loading')return;
 if(g!==gen||document.hidden){item.state='waiting';loadThumbnails();return;}
 const follow=item.x.markdown&&atBottom()&&!selectionInRecord();
 if(value?.ok===true&&pngSource(value)){
  if(item.x.markdown){item.image.width=value.width;item.image.height=value.height;}
  paintImage(item.image,value);item.image.hidden=false;item.label.textContent=item.alt;
  if(item.x.markdown)item.hint.hidden=true;
  else item.hint.textContent='タップして拡大';
  item.state='done';
 }else thumbnailFailed(item);
 // Keep only a small number of decoded thumbnails as the reader scrolls.
 const loaded=[...thumbnails.values()].filter(x=>x.state==='done');
 for(const old of loaded){if(loaded.filter(x=>x.state==='done').length<=24)break;if(!old.visible){clearImage(old.image);old.image.hidden=true;old.state='waiting';}}
 if(follow)list.scrollTop=list.scrollHeight;
 loadThumbnails();
}
/* Checked again here: a PNG data URL from strict base64 within the relay's
   side and pixel limits. */
function pngSource(v){
 return v&&v.kind==='image'&&(v.mime===undefined||v.mime==='image/png')&&typeof v.data==='string'&&v.data.length<=MAX_PREVIEW_BYTES&&BASE64.test(v.data)&&v.data.startsWith('iVBORw0KGgo')
  &&Number.isInteger(v.width)&&Number.isInteger(v.height)&&v.width>0&&v.height>0&&v.width<=MAX_SIDE&&v.height<=MAX_SIDE&&v.width*v.height<=MAX_PIXELS
  ?'data:image/png;base64,'+v.data:null;
}
function closePreview(restoreFocus=false){
 controllers.preview?.abort();controllers.preview=null;dropSave();
 previewRequest=null;previewRetry=false;
 if(panel){for(const img of panel.querySelectorAll('img'))clearImage(img);panel.replaceChildren();panel.remove();}
 panel=null;
 if(restoreFocus&&previewFocus?.isConnected)previewFocus.focus?.({preventScroll:true});previewFocus=null;
}
function dismissPreview(){if(!panel)return false;closePreview(true);return true;}
/* A plain file name: the last path part, without control or bidi-override
   characters, at most 120 characters. Images are saved as the PNG cockpit
   re-drew, so they end in .png; text that a viewer could render as markup
   (.html, .htm, .xhtml, .svg, .xml) gets .txt, matching the plain-text bytes. */
function fileName(label,image){
 let n=String(label||'').split(/[\\/]/).pop().replace(/[\u0000-\u001f\u007f-\u009f\u200e\u200f\u202a-\u202e\u2066-\u2069]/g,'').trim().slice(0,120);
 if(!n||n==='.'||n==='..')n=image?'image':'file.txt';
 if(image)return /\.png$/i.test(n)?n:n+'.png';
 return /\.(?:html?|xhtml|svg|xml)$/i.test(n)?n+'.txt':n;
}
function dropSave(){if(saveUrl){URL.revokeObjectURL(saveUrl);saveUrl='';}}
/* Saves the bytes this preview already holds; no further request is made. */
function saveButton(head,close,label,blob){
 const b=make('button','conversation-preview-save','Save');b.type='button';
 b.onclick=()=>{
  if(!panel)return;dropSave();saveUrl=URL.createObjectURL(blob());
  const a=make('a');a.download=fileName(label,blob.image);a.href=saveUrl;a.hidden=true;
  panel.append(a);a.click();a.remove();
 };
 head.insertBefore(b,close);
}
function markdownPreview(text,head,close,view){
 const source=make('textarea','conversation-preview-text conversation-preview-source');
 source.setAttribute('aria-label','Markdown source');
 source.autocapitalize='off';source.spellcheck=false;source.value=text;
 // A textarea normalizes line endings. Keep the original bytes for an unchanged download.
 const original=source.value,read=()=>source.value===original?text:source.value;
 function draw(value){
  const doc=body(value,true);doc.classList.add('conversation-preview-markdown');return doc;
 }
 let rendered=text,doc=draw(text);
 view.append(doc);
 const toggle=make('button','icon-button conversation-preview-toggle');toggle.type='button';
 const namespace='http://www.w3.org/2000/svg';
 const svg=document.createElementNS(namespace,'svg'),path=document.createElementNS(namespace,'path');
 svg.setAttribute('class','mobile-icon');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');
 path.setAttribute('d','m8 6-6 6 6 6m8-12 6 6-6 6');
 svg.append(path);toggle.append(svg);
 function label(editing){
  toggle.title=editing?'Show rendered Markdown':'Edit Markdown source';
  toggle.setAttribute('aria-label',toggle.title);
  toggle.classList.toggle('active',editing);
 }
 label(false);
 toggle.onclick=()=>{
  const editing=!source.isConnected;
  const value=read();
  if(!editing&&rendered!==value){rendered=value;doc=draw(value);}
  view.replaceChildren(editing?source:doc);
  view.scrollTop=0;
  label(editing);
  if(editing)source.focus({preventScroll:true});
 };
 head.insertBefore(toggle,close);
 return read;
}
function pngBytes(data){const raw=atob(data),out=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)out[i]=raw.charCodeAt(i);return out;}
async function openPreview(x){
 closePreview();if(!root)return;
 previewRequest=x;
 previewFocus=document.activeElement;
 const g=gen,box=make('div','conversation-preview');panel=box;box.setAttribute('role','dialog');box.setAttribute('aria-label','Preview of '+x.label);
 const head=make('div','conversation-preview-head'),close=make('button','conversation-preview-close','Close');close.type='button';close.onclick=()=>closePreview(true);
 head.append(make('strong','',x.label),close);
 const state=make('p','conversation-preview-status','Loading preview…');state.setAttribute('role','status');
 const view=make('div','conversation-preview-body');box.append(head,state,view);root.append(box);
 close.focus?.({preventScroll:true});
 const c=new AbortController();controllers.preview=c;let late=false;const t=setTimeout(()=>{late=true;c.abort();},WAIT);
 let v=null,status=0;
 try{
  const r=await fetch('/view/conversation/preview?name='+encodeURIComponent(name)+'&ref='+x.ref,
   {credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer',signal:c.signal});
  status=r.status;const raw=await r.text();if(raw.length>MAX_PREVIEW_BYTES)throw Error('unavailable');v=JSON.parse(raw);
 }catch(_){v=late?{ok:false,error:'history_slow'}:null;}
 finally{clearTimeout(t);if(controllers.preview===c)controllers.preview=null;}
 if(g!==gen||panel!==box)return;
 previewRequest=null;
 if(status===410)v={ok:false,error:'preview_expired'};
 if(v?.ok===true&&v.kind==='text'&&typeof v.text==='string'&&!x.markdown){
  state.textContent=v.truncated===true?'Showing the start of the file.':'';
  const text=v.text;
  let read=()=>text;
  if(/\.(?:md|markdown)$/i.test(x.label)&&markdown())read=markdownPreview(text,head,close,view);
  else view.append(make('pre','conversation-preview-text',text));
  saveButton(head,close,x.label,()=>new Blob([read()],{type:'text/plain;charset=utf-8'}));
 }else if(v?.ok===true&&pngSource(v)){
  const img=make('img','conversation-preview-image');img.alt=x.label;img.width=v.width;img.height=v.height;
  paintImage(img,v);state.textContent='';view.append(img);
  const data=v.data,blob=()=>new Blob([pngBytes(data)],{type:'image/png'});blob.image=true;saveButton(head,close,x.label,blob);
 }else{
  if(x.markdown)for(const item of thumbnails.values())if(item.x.markdown&&item.x.ref===x.ref){
   item.controller?.abort();thumbnailFailed(item);
  }
  state.textContent=previewMessages[v?.error]||messages[v?.error]||'Preview unavailable right now.';return;
 }
 if(typeof v.notice==='string'&&v.notice)box.append(make('small','conversation-preview-notice',v.notice));
}
function atBottom(){return !list||replyMotion()?.followingReply()||(list.isConnected?list.scrollHeight-list.clientHeight-list.scrollTop<=24:following);}
/* The sheet rebuilds its children; detaching resets scrollTop. Restore the
   reader's place (or the tail) after each reattachment. */
function restore(){
 if(!list||!list.isConnected)return;
 stepView?.activity(stepContext(),stepsVisible());
 if(!replyMotion()?.keepReplyInView())list.scrollTop=following?list.scrollHeight:savedTop;
 loadThumbnails();
}
function setStatus(text){if(!status)return;status.textContent=text;
 // Routine explanations live in Information; failures remain on the surface.
 status.hidden=/^(?:Read-only record · updating|Latest record · updating|New messages below · paused while you read)$/.test(text);
}
function activity(){
 if(list&&events.length&&stepView?.needsSync(stepContext()))regroupTools();
 if(!list)return;
 const a=window.OrreryMobileAgent?.forName(name),context=stepContext(a);
 const stepActivity=stepView?.activity(context,stepsVisible()),compacting=context.reader_phase==='compacting';
 list.querySelector('.conversation-activity')?.remove();
 const box=make('div','conversation-activity');
 if(compacting){
  const elapsed=context.reader_phase_elapsed;
  box.textContent='Compacting context…'+(elapsed?' · '+elapsed:'');
 }else if(context.running&&a.state==='work'&&!stepActivity)box.textContent='Working'+(a.workDisplay?' · '+a.workDisplay:'');
 else{if(context.running&&['ask','question'].includes(a.state)&&!stepActivity){box.className+=' reply-needed';box.textContent=a.state==='ask'?'Reply needed · approval':'Reply needed · question';}else if(a?.stale)box.textContent='Activity observation unavailable';}
 if(box.textContent)list.append(box);
 const dot=document.querySelector?.('.reader-state');if(dot&&root?.isConnected){dot.dataset.state=a?.stale?'unknown':a?.state||'unknown';dot.title=a?.stale?'Activity observation unavailable':a?.state==='work'?'Working':a?.state==='ask'?'Approval needed':a?.state==='question'?'Reply needed':'Waiting';dot.setAttribute('aria-label',dot.title);}
}

function releasePendingRow(item){
 const entry=pendingRows.get(item);
 if(!entry)return;
 for(const url of entry.urls)URL.revokeObjectURL(url);
 pendingRows.delete(item);
}
function keepPending(keep){
 pending=pending.filter(item=>{
  if(keep(item))return true;
  releasePendingRow(item);return false;
 });
}
function renderPending(){
 if(!list)return;
 const kept=new Set();
 let addedReference=false;
 for(const p of pending){
  let entry=pendingRows.get(p);
  if(!entry){
   const row=make('div','conversation-bubble user conversation-pending');
   const reference=replyReference(p.displayText,p.attachments.length>0);
   let wash=row;
   if(reference){
    row.classList.add('conversation-has-reference');wash=make('div','conversation-reference-body');
    row.append(referenceCard(reference),wash);
    addedReference=true;
   }
   const label=make('small','conversation-time'),dismiss=make('button','conversation-dismiss','Dismiss');
   label.setAttribute('role','status');dismiss.type='button';
   dismiss.onclick=()=>{list.focus({preventScroll:true});keepPending(x=>x!==p);renderPending();activity();};
   const visibleText=reference?.reply??p.displayText;
   if(visibleText)wash.append(body(visibleText));
   const images=make('div','conversation-shown-files'),files=make('div','conversation-files'),urls=[];
   images.dataset.role='user';
   for(const attachment of p.attachments){
    if(attachment.kind==='image'&&attachment.file instanceof File){
     const url=URL.createObjectURL(attachment.file),tile=make('span','conversation-thumbnail');
     const img=make('img','conversation-thumbnail-image');img.src=url;img.alt=attachment.label;
     urls.push(url);tile.append(img);images.append(tile);
    }else files.append(chip(attachment));
   }
   if(images.childElementCount)wash.append(images);
   if(files.childElementCount)wash.append(files);
   wash.append(label,dismiss);
   entry={row,label,dismiss,urls};pendingRows.set(p,entry);
  }
  const {row,label,dismiss}=entry;
  kept.add(row);
  const status=p.accepted?'Sent to terminal · not yet in the record':'Sending…';
  if(label.textContent!==status)label.textContent=status;
  row.dataset.state=p.accepted?'accepted':'sending';dismiss.hidden=!p.accepted;
  if(row.parentElement!==list)list.insertBefore(row,list.querySelector(':scope > .conversation-activity'));
 }
 for(const row of list.querySelectorAll(':scope > .conversation-pending')){
  if(!kept.has(row)){if(row.contains(document.activeElement))list.focus({preventScroll:true});row.remove();}
 }
 if(addedReference)updateReferenceCards();
}
function settlePending(){
 const now=Date.now();
 keepPending(p=>now-p.at<180000);
 const landings=[];
 for(const event of events){
  if(!pending.length)break;
  if(event.role!=='user'||event.kind!=='text'||matchedSends.has(event.id))continue;
  const text=(markdown()?.submissionText(event.text)||event.text).trim();
  const item=pending.find(p=>!p.before.has(event.id)&&text===p.text.trim());
  if(!item)continue;
  const from=replyMotion()?.promptPosition(pendingRows.get(item)?.row);
  const record=list.querySelector('[data-id="'+CSS.escape(event.id)+'"]');
  if(from&&record)landings.push({record,from});
  item.recorded=true;matchedSends.add(event.id);keepPending(p=>p!==item);
 }
 return landings;
}
function flyLandings(landings){
 for(const {record,from} of landings)replyMotion()?.flyPrompt(record,from,from.remaining);
}
function groupShellRows(){
 let owner=null;
 for(const e of events){
  const command=e.role==='user'&&markdown()?.commandRecord(e.text);
  if(!command?.shell){owner=null;continue;}
  const node=list.querySelector('[data-id="'+CSS.escape(e.id)+'"]');if(!node)continue;
  if(command.command!==null||!owner){owner=node;continue;}
  if(node.parentElement===owner)continue;
  const focus=document.activeElement,held=node.contains?.(focus);
  if(held)list.focus?.({preventScroll:true});
  node.className='conversation-shell-part';node.querySelector('.conversation-speaker')?.remove();owner.append(node);
  if(held&&focus.isConnected)focus.focus({preventScroll:true});
 }
}
function setToolGroupOpen(group,open){
 const top=group.button.getBoundingClientRect().top;
 group.open=open;group.button.setAttribute('aria-expanded',String(open));
 group.button.querySelector('.conversation-tool-arrow').textContent=open?'▾':'▸';
 for(const row of group.rows){if(!open&&row.contains?.(document.activeElement))group.button.focus?.({preventScroll:true});row.hidden=!open;}
 list.scrollTop+=group.button.getBoundingClientRect().top-top;
 following=atBottom();savedTop=list.scrollTop;
}
function regroupTools(focused=toolGroups.find(g=>g.button===document.activeElement)){
 const stepRows=stepView?.sync({events,provider,rows:[...list.children].filter(r=>r.dataset.id!==undefined),context:stepContext(),isVisible:stepsVisible()})||new Set();
 const previous=toolGroups,used=new Set(),groups=[],byId=new Map(events.map(e=>[e.id,e]));let run=[];
 const finish=()=>{
  if(!run.length)return;
  const matches=[...new Set(run.map(r=>toolGroupFor.get(r.dataset.id)).filter(Boolean))];
  let group=matches.find(g=>g===focused&&!used.has(g))||matches.find(g=>!used.has(g));
  const open=matches.some(g=>g.open);
  if(!group){
   const button=make('button','conversation-tool-group');button.type='button';button.dataset.toolGroup=String(++toolSerial);
   const arrow=make('span','conversation-tool-arrow','▸');arrow.setAttribute('aria-hidden','true');
   button.append(arrow,make('span','conversation-tool-count'),make('span','conversation-tool-names'),make('span','conversation-tool-times'));
   group={button,open:false,rows:[]};button.onclick=()=>setToolGroupOpen(group,!group.open);
  }
  used.add(group);group.rows=run;group.open=open;
  const calls=run.map(r=>byId.get(r.dataset.id)),counts=new Map();
  for(const event of calls)counts.set(event.text,(counts.get(event.text)||0)+1);
  const names=[...counts].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])).map(([n,count])=>(n||'Tool')+' ×'+count).join(', ');
  const label=calls.length+' tool call'+(calls.length===1?'':'s'),dates=calls.map(e=>new Date(e.ts)).filter(d=>Number.isFinite(d.getTime())).sort((a,b)=>a-b);
  const times=dates.length?clock(dates[0])+(dates.length>1?'–'+clock(dates[dates.length-1]):''):'';
  const full=label+(names?' · '+names:'')+(dates.length?' · '+dates[0].toLocaleString()+(dates.length>1?' – '+dates[dates.length-1].toLocaleString():''):'');
  const button=group.button;button.title=full;button.setAttribute('aria-label',full);button.setAttribute('aria-expanded',String(open));
  button.querySelector('.conversation-tool-arrow').textContent=open?'▾':'▸';
  button.querySelector('.conversation-tool-count').textContent=label;
  button.querySelector('.conversation-tool-names').textContent=names?'· '+names:'';
  button.querySelector('.conversation-tool-times').textContent=times;
  if(button.nextElementSibling!==run[0])list.insertBefore(button,run[0]);
  for(const row of run)row.hidden=!open;
  groups.push(group);run=[];
 };
 for(const row of [...list.children]){
  if(row.className==='conversation-shown-files'){finish();continue;}
  if(row.dataset.id===undefined)continue;
  const e=byId.get(row.dataset.id);
  if(row.className==='conversation-compact-envelope'||compactRecord(e)){finish();continue;}
  if(e&&stepRows.has(e.id)){finish();row.classList.add('conversation-step-record');row.hidden=!row.querySelector('.conversation-files')&&!row.querySelector('.conversation-mail');continue;}
  if(row.className.includes('conversation-step-record'))row.className=row.className.replace(' conversation-step-record','');
  if(e?.kind==='tool'&&!e.mail?.length)run.push(row);else{finish();row.hidden=false;}
 }
 finish();
 for(const g of previous)if(!used.has(g)){if(g.button===document.activeElement)list.focus?.({preventScroll:true});g.button.remove();}
 toolGroups=groups;toolGroupFor.clear();for(const g of groups)for(const r of g.rows)toolGroupFor.set(r.dataset.id,g);
 if(focused&&(document.activeElement===document.body||document.activeElement===list))(groups.includes(focused)?focused.button:list).focus?.({preventScroll:true});
}
function clearToolGroups(){
 for(const g of toolGroups){if(g.button===document.activeElement)list?.focus?.({preventScroll:true});g.button.remove();}
 toolGroups=[];toolGroupFor.clear();toolSerial=0;
}
function dropEvent(id){
 const shown=list.querySelector('[data-shown-for="'+CSS.escape(id)+'"]');if(shown){forgetThumbnails(shown);shown.remove();}
 const node=list.querySelector('[data-id="'+CSS.escape(id)+'"]');if(!node)return;
 // A group can outlive its oldest member at the 200-event limit. Promote
 // retained output before removing that member, so its text is not lost.
 for(const part of [...node.children].filter(x=>x.dataset.id!==undefined)){
  part.className='conversation-shell';part.insertBefore(make('small','conversation-speaker','SHELL'),part.firstChild);node.parentNode.insertBefore(part,node);
 }
 forgetThumbnails(node);
 if(node.contains?.(document.activeElement))list.focus?.({preventScroll:true});node.remove();
}
function append(page,replySince){
 const follow=atBottom()&&!selectionInRecord(),fresh=page.filter(e=>!seen.has(e.id));
 for(const item of pending){
  if(!item.awaitingBaseline)continue;
  // When sending from a detached Terminal, the first Reader page can
  // include an older submission with exactly the same text.
  const earliest=Math.floor(item.at/1000)*1000;
  for(const event of page)if(!(Date.parse(event.ts)>=earliest))item.before.add(event.id);
  item.awaitingBaseline=false;
 }
 const motion=follow&&replySince!==null&&replyMotion();
 // Use the known tail and the poll time to avoid animating backfilled records.
 const arrivals=motion?new Set(page.slice(page.findLastIndex(e=>seen.has(e.id))+1)
  .filter(e=>Date.parse(e.ts)>=replySince)):null;
 const animations=[];
 for(const e of fresh){
  seen.add(e.id);events.push(e);
  const anchor=list.querySelector(':scope > .conversation-pending'),node=row(e);
  list.insertBefore(node,anchor);
  if(arrivals?.has(e))animations.push({event:e,node});
  const shown=shownFiles(e);if(shown)list.insertBefore(shown,anchor);
 }
 let dropped=false;
 while(events.length>MAX_KEPT){const gone=events.shift();seen.delete(gone.id);matchedSends.delete(gone.id);dropEvent(gone.id);older=null;dropped=true;}
 groupShellRows();
 regroupTools();
 refreshProfiles();
 // An omitted marker belongs to its page's old edge. Only when that edge's
 // messages are dropped at the kept limit does a marker above them go too.
 if(dropped)for(const n of [...list.children]){if(n.dataset?.id!==undefined)break;if(n.className==='conversation-omitted')n.remove();}
 const landings=settlePending();renderPending();activity();
 for(const {event,node} of animations){
  if(event.kind==='text'&&event.role==='assistant'&&event.variant!=='update')motion.revealReply(node.querySelector('.conversation-text'),list);
  if(event.mail?.length)motion.revealMail(node);
 }
 if(follow){following=true;if(!replyMotion()?.keepReplyInView())list.scrollTop=list.scrollHeight;}
 else if(fresh.length)setStatus('New messages below · paused while you read');
 flyLandings(landings);
 loadThumbnails();
}
/* An older page is oldest-first. At the kept limit keep its newest part, the
   part next to what is shown, and stop offering older pages so no gap opens. */
/* Marks a gap at the old edge of the rows just added. */
function gap(){const n=make('div','conversation-omitted',messages.omitted);n.setAttribute('role','note');return n;}
function prepend(page,omitted){
 const before=list.scrollHeight,first=[...list.children].find(x=>x!==olderButton)||null;
 let fresh=page.filter(e=>!seen.has(e.id));const room=Math.max(0,MAX_KEPT-events.length);
 if(fresh.length>room){fresh=room?fresh.slice(-room):[];older=null;setStatus(messages.kept_limit);}
 if(omitted)list.insertBefore(gap(),first);
 for(const e of fresh){seen.add(e.id);list.insertBefore(row(e),first);const shown=shownFiles(e);if(shown)list.insertBefore(shown,first);}
 events=fresh.concat(events);
 groupShellRows();
 regroupTools();
 refreshProfiles();
 list.scrollTop+=list.scrollHeight-before;following=false;savedTop=list.scrollTop;
 loadThumbnails();
}
function reset(){
 replyMotion()?.finishReplies();matchedSends.clear();
 stepView?.reset();provider='';lastRecordPoll=null;recordVersion++;
 controllers.older?.abort();olderLoad=null;referenceJump=null;clearReferenceFlash();
 dismissReferenceSelection();closePreview();forgetThumbnails();clearToolGroups();
 events=[];seen.clear();older=null;
 list.querySelectorAll(':scope > .conversation-shown-files').forEach(x=>x.remove());
 list.querySelectorAll(':scope > [data-id]').forEach(x=>x.remove());
 list.querySelectorAll(':scope > .conversation-omitted').forEach(x=>x.remove());
 window.OrreryMobileTerminal?.refreshReference?.();
}
function slow(on){if(retryButton)retryButton.hidden=!on;if(on)setStatus(messages.history_slow);}
function showError(v){
 const code=typeof v?.error==='string'?v.error:'history_unavailable';
 if(code==='history_slow'){slow(true);return;}
 const reason=typeof v?.reason_code==='string'&&/^[a-z0-9_]{1,64}$/.test(v.reason_code)?' ('+v.reason_code+')':'';
 setStatus((messages[code]||'History is unavailable right now.')+reason);
}
async function poll(){
 const g=gen;clearTimeout(timer);timer=0;
 if(!root||document.hidden)return;
 const started=Date.now();
 try{
  const v=await request(null,'poll');if(g!==gen)return;
  if(v.ok!==true){
   // A hidden, stale or changed record also hides any file opened from it;
   // a slow answer is only a delay and leaves it open.
   if(v.error!=='history_slow'){recordConfirmed=false;lastRecordPoll=null;recordVersion++;dismissReferenceSelection();window.OrreryMobileTerminal?.refreshReference?.();closePreview();stepView?.closeDetails();}showError(v);if(v.error==='history_changed')reset();}
  else{
   recordConfirmed=true;provider=({claude:'Claude',codex:'Codex',dsh:'dsh',hermes:'Hermes',antigravity:'Antigravity'})[v.provider]||'';
   failures=0;slow(false);if(!events.length&&v.omitted===true)list.insertBefore(gap(),list.querySelector(':scope > .conversation-pending'));
   const replySince=lastRecordPoll===null?null:Math.max(lastRecordPoll,started-2*POLL)-POLL;
   append(v.events,replySince);lastRecordPoll=started;
   // The latest page's older token stays valid only while the oldest shown
   // message is still in that page; it is renewed on every such poll.
   if(events.length&&v.events.some(e=>e.id===events[0].id))older=v.older;
   if(atBottom())setStatus(v.events.length?(v.tail_only?'Latest record · updating':'Read-only record · updating'):'No messages in the record yet.');
   if(v.limited&&!events.length)setStatus('The latest record was shortened.');
   olderButton.hidden=!older||Boolean(olderLoad);
   selectionReply();window.OrreryMobileTerminal?.refreshReference?.();
  }
  updateReferenceCards();
 }catch(e){if(g!==gen||e?.superseded)return;failures++;if(e?.slow)slow(true);else setStatus('History unavailable. Retrying.');}
 if(g===gen&&root&&!document.hidden)timer=setTimeout(poll,failures?Math.min(30000,POLL*2**failures):POLL);
}
function loadOlder(){
 if(olderLoad||!older)return olderLoad;
 const g=gen,version=recordVersion,token=older;olderButton.hidden=true;
 const current=()=>g===gen&&version===recordVersion;
 const load=(async()=>{
  try{
   const v=await request(token,'older');if(!current())return false;
   if(v.ok!==true&&v.error==='page_expired'){
    // Start again from the latest page, which brings a fresh token.
    reset();await poll();if(g===gen)setStatus(messages.page_expired);return false;
   }
   // A slow answer keeps the token, so Show older can simply be tapped again.
   if(v.ok===true){older=v.older;prepend(v.events,v.omitted===true);return true;}
   if(v.error==='history_slow')setStatus(messages.older_slow);
   else{showError(v);older=null;}
  }catch(e){if(current()&&!e?.superseded)setStatus(e?.slow?messages.older_slow:'Older messages unavailable.');}
  finally{if(olderLoad===load){olderLoad=null;olderButton.hidden=!older;updateReferenceCards();}}
  return false;
 })();
 olderLoad=load;return load;
}
function mount(n){
 unmount();name=n;root=make('div','conversation');root.id='conversation';
 status=make('p','conversation-status','Loading the record…');status.setAttribute('role','status');
 olderButton=make('button','conversation-older','Show older');olderButton.type='button';olderButton.hidden=true;olderButton.onclick=loadOlder;
 list=make('div','conversation-list');list.tabIndex=-1;list.setAttribute('aria-label','Conversation record for '+n);
 list.addEventListener('scroll',()=>{if(!list.isConnected)return;following=atBottom();savedTop=list.scrollTop;if(following&&/paused/.test(status.textContent))setStatus('Read-only record · updating');selectionReply();},{passive:true});
 for(const type of ['touchstart','wheel'])list.addEventListener(type,()=>replyMotion()?.stopFollowing(),{passive:true});
 list.addEventListener('keydown',event=>{
  if(['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '].includes(event.key))replyMotion()?.stopFollowing();
 });
 retryButton=make('button','conversation-retry','Retry');retryButton.type='button';retryButton.hidden=true;
 retryButton.onclick=()=>{retryButton.hidden=true;setStatus('Loading the record…');return poll();};
 list.append(olderButton);root.append(status,retryButton,list);
 stepView=window.OrreryMobileSteps.mount({host:list,loadDetail:loadStepDetail,previewImage:openPreview,
  mutate:(fn,button)=>{const top=button.getBoundingClientRect().top;fn();list.scrollTop+=button.getBoundingClientRect().top-top;following=atBottom();savedTop=list.scrollTop;},
  onFocusLost:()=>list.focus?.({preventScroll:true})});
 if(typeof ResizeObserver==='function'){mailResize=new ResizeObserver(()=>{if(list&&list.clientWidth!==mailWidth){mailWidth=list.clientWidth;layoutMailRoutes();}});mailResize.observe(list);}
 const g=gen;document.fonts?.ready?.then(()=>{if(g===gen)layoutMailRoutes();});poll();return root;
}
function element(){return root;}
function beginPending(text,origin,display,accepted=false,at=Date.now()){
 if(!root||typeof text!=='string'||!text.trim())return null;
 const item={text,displayText:display?.text??text,attachments:(display?.attachments||[]).slice(0,8),
  at,before:new Set(seen),owner:root,accepted,awaitingBaseline:!recordConfirmed};
 pending.push(item);renderPending();following=true;restore();
 if(origin)replyMotion()?.flyPrompt(pendingRows.get(item)?.row,origin);
 return item;
}
function finishPending(item,accepted){
 if(!item||item.owner!==root||item.recorded)return;
 if(accepted){
  if(!pending.includes(item))pending.push(item);
  item.accepted=true;
 }else keepPending(p=>p!==item);
 const landings=settlePending();renderPending();restore();flyLandings(landings);
}
function addPending(text,origin,display,at){
 const item=beginPending(text,origin,display,true,at);if(item)finishPending(item,true);
}
function unmount(){
 replyMotion()?.finishReplies();matchedSends.clear();
 stepView?.reset();stepView=null;provider='';
 recordVersion++;recordConfirmed=false;lastRecordPoll=null;referenceJump=null;clearReferenceFlash();dismissReferenceSelection();
 gen++;clearTimeout(timer);timer=0;closePreview();forgetThumbnails();for(const k in controllers){controllers[k]?.abort();controllers[k]=null;}
 mailResize?.disconnect();mailResize=null;mailWidth=0;
 clearToolGroups();
 root?.replaceChildren();root?.remove();root=list=status=olderButton=retryButton=null;
 events=[];seen=new Set();older=null;keepPending(()=>false);failures=0;olderLoad=null;following=true;savedTop=0;name='';
}
document.addEventListener('visibilitychange',()=>{
 if(document.hidden){
  lastRecordPoll=null;
  stepView?.activity(stepContext(),false);
  stepView?.closeDetails();
  gen++;clearTimeout(timer);timer=0;
  if(controllers.preview&&previewRequest)previewRetry=true;
  for(const item of thumbnails.values())item.controller?.abort();
  for(const k in controllers){controllers[k]?.abort();controllers[k]=null;}
  referenceJump=null;clearReferenceFlash();olderLoad=null;if(olderButton)olderButton.hidden=!older;
 }else if(root){
  if(previewRetry&&previewRequest&&panel){const x=previewRequest,focus=previewFocus;openPreview(x);previewFocus=focus;}
  restore();poll();
 }
});window.addEventListener('pagehide',unmount);
window.OrreryMobileConversation={enabled,mount,element,unmount,addPending,beginPending,finishPending,restore,dismissPreview,refreshActivity:activity,refreshProfiles,dismissReferenceSelection};
})();
