"""Drive the real mobile Terminal IIFE with finite previews and a fake clock.

The DOM model clamps scrollTop when content shrinks, as a browser does. No
real backend, identity, terminal input or wall-clock polling is involved.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const staticElements=new Map(),timers=new Map(),calls=[],frames=[];let seq=0,lines=40,shift=0,updates=0,failPreview=false,truncated=false,controls=false;
let viewHeight=200,viewWidth=350,rowHeight=20,holdPreview=false,releasePreview=null,emptyPreview=false;
class Element{
 constructor(tag='div',cls=''){this.tag=tag;this.className=cls;this.children=[];this.dataset={};this.style={};this.events={};this.classList={toggle(){}};this.isConnected=true;this.open=false;this._text='';this._top=0;}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 set textContent(value){this._text=String(value);this.children=[];this.scrollTop=this._top;}
 get clientHeight(){return this.tag==='pre'?viewHeight:0;}get clientWidth(){return viewWidth;}
 get scrollHeight(){return this.tag==='pre'?Math.max(viewHeight,this.textContent.split('\n').length*rowHeight):0;}
 get scrollTop(){return this._top;}set scrollTop(value){this._top=Math.max(0,Math.min(value,this.scrollHeight-this.clientHeight));}
 append(...values){this.children.push(...values);}replaceChildren(...values){this._text='';this.children=[...values];}
 setAttribute(){}addEventListener(kind,fn){this.events[kind]=fn;}focus(){}querySelectorAll(){return [];}querySelector(){return null;}after(){}remove(){}
 showModal(){this.open=true;}close(){this.open=false;}
}
for(const s of ['#terminal-sheet','#selected-name','.selected-agent','#attention','#events','body>header>span'])staticElements.set(s,new Element());
staticElements.get('#selected-name').textContent='ScrollFixture';
function find(selector,root=staticElements.get('#terminal-sheet')){
 const match=selector[0]==='#'?root.id===selector.slice(1):(root.className||'').split(' ').includes(selector.slice(1));
 if(match)return root;for(const child of root.children){const got=find(selector,child);if(got)return got;}return null;
}
const q=s=>staticElements.get(s)||find(s);
const doc={querySelector:q,querySelectorAll:()=>[],createElement:t=>new Element(t),events:{},addEventListener(kind,fn){this.events[kind]=fn;},hidden:false,activeElement:new Element()};
const target=c=>({name:'ScrollFixture',pane_label:'Pane '+c,choice:String(c).repeat(64),revision:'d'.repeat(64)});
const context={document:doc,console,TextEncoder,AbortController,MutationObserver:class{observe(){}},innerHeight:844,
 requestAnimationFrame:fn=>{frames.push(fn);return frames.length;},
 setTimeout(fn,ms){timers.set(++seq,{fn,ms});return seq;},clearTimeout(id){timers.delete(id);},
 fetch:async(url,options)=>{
  const op=url.split('/').pop(),payload=options.body?JSON.parse(options.body):null;
  calls.push({op,payload});
  let value={contract:'orrery-mobile-pane-v1',epoch:'b'.repeat(32)};
  if(!payload)value={...value,api:31,supported:'legacy-managed-pane',enabled:true,ack:'tmux-command-accepted'};
  else if(op==='targets')value={...value,targets:[target(1),target(2)],truncated:false};
  else if(op==='preview'){
   if(failPreview)throw Error('fixture offline');
   if(holdPreview)await new Promise(resolve=>releasePreview=resolve);
   value={...value,target:target(payload.choice[0]),text:emptyPreview?'':Array.from({length:lines},(_,i)=>'line '+(i+shift)).join('\n')+'\nupdate '+updates,observed_at:1+updates,truncated,controls_removed:controls};
  }else value={...value,target:target(1),intent:'e'.repeat(64),state:op==='prepare'?'prepared':op==='commit'?'accepted':'cancelled',text:payload.text,expires_in:30,confirmation:'f'.repeat(64)};
  return {ok:true,text:async()=>JSON.stringify(value)};
 }};
context.window=context;context.addEventListener=()=>{};context.scrollTo=()=>{};context.AGENTSTACK_DEMO={portraitURL:()=>'/fixture.svg'};
vm.runInNewContext(SOURCE,context);
const settle=async()=>{for(let i=0;i<15;i++)await new Promise(r=>setImmediate(r));};
function flushFrames(){frames.splice(0).forEach(fn=>fn());}
function button(label,root=q('#terminal-sheet')){if(root.tag==='button'&&root.textContent===label)return root;for(const child of root.children){const got=button(label,child);if(got)return got;}}
async function tick(){const entry=[...timers].find(([,t])=>t.ms===3000);assert.ok(entry,'preview poll scheduled');timers.delete(entry[0]);await entry[1].fn();await settle();}
async function open(c=1){await context.OrreryMobileTerminal.open('ScrollFixture');await settle();button('Pane '+c+'ScrollFixture').onclick();await settle();}
const bottom=p=>p.scrollHeight-p.clientHeight;
(async()=>{await settle();await open();BODY;context.OrreryMobileTerminal.close();console.log('ok');})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def run(body):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is unavailable')
    source = (ROOT / 'dashboard/mobile/mobile.js').read_text()
    source = '/* Common, opt-in pane terminal.' + source.split('/* Common, opt-in pane terminal.', 1)[1]
    script = HARNESS.replace('SOURCE', json.dumps(source)).replace('BODY', body)
    result = subprocess.run([node, '-'], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'


@pytest.mark.parametrize('changed', [False, True])
def test_preview_polls_preserve_the_readers_scroll_position(changed):
    run("""
      const p=q('#terminal-output');assert.equal(p.scrollTop,bottom(p));p.scrollTop=100;
      for(let i=0;i<4;i++){
        CHANGE;await tick();assert.equal(q('#terminal-output'),p);
        assert.equal(p.scrollTop,100,'poll must not move a reader');
      }
    """.replace('CHANGE', 'updates++;lines++;' if changed else ''))


@pytest.mark.parametrize('distance', [0, 1, 2, 3, 20])
def test_only_a_view_at_the_bottom_follows_new_output(distance):
    run("""
      const p=q('#terminal-output');const top=bottom(p)-DISTANCE;p.scrollTop=top;
      lines+=3;updates++;await tick();
      assert.equal(p.scrollTop,DISTANCE<=2?bottom(p):top);
    """.replace('DISTANCE', str(distance)))


def test_review_rerenders_keep_the_reading_position():
    run("""
      q('#terminal-output').scrollTop=100;
      const reading=q('#terminal-output').textContent;shift+=3;await tick();
      const field=q('#terminal-text');field.value='synthetic one line';field.oninput();
      await button('Review input').onclick();await settle();
      assert.equal(q('#terminal-output').scrollTop,100,'review must keep reader position');
      assert.equal(q('#terminal-output').textContent,reading);
      button('Edit').onclick();await settle();assert.equal(q('#terminal-output').scrollTop,100);
    """)


def test_rolling_tail_keeps_the_visible_text_until_the_reader_returns_to_bottom():
    run("""
      const p=q('#terminal-output');p.scrollTop=100;const reading=p.textContent;
      for(let i=0;i<3;i++){
        shift+=3;updates++;await tick();
        assert.equal(p.scrollTop,100);assert.equal(p.textContent,reading,'rolling tail must not replace the line being read');
      }
      assert.equal(q('.terminal-stream').textContent,'New output · paused');
      p.scrollTop=bottom(p);assert.equal(typeof p.events.scroll,'function');p.events.scroll();
      assert.notEqual(p.textContent,reading);
      assert.ok(p.textContent.startsWith('line 9\\n'));assert.equal(p.scrollTop,bottom(p));
      assert.equal(q('.terminal-stream').textContent,'Updating every 3s');
    """)


def test_returning_to_bottom_resumes_follow_even_when_latest_content_is_shorter():
    run("""
      const p=q('#terminal-output');p.scrollTop=100;await tick();assert.equal(p.scrollTop,100);
      p.scrollTop=bottom(p);lines+=3;await tick();assert.equal(p.scrollTop,bottom(p));
      p.scrollTop=400;lines=15;await tick();assert.equal(p.scrollTop,400);
      p.scrollTop=bottom(p);await tick();assert.equal(p.scrollTop,bottom(p));
    """)


def test_pane_switch_and_reopen_start_at_the_new_tail():
    run("""
      q('#terminal-output').scrollTop=100;
      await button('Pane 1 ⌄').onclick();await settle();button('Pane 2ScrollFixture').onclick();await settle();
      assert.equal(q('#terminal-output').scrollTop,bottom(q('#terminal-output')));
      q('#terminal-output').scrollTop=100;context.OrreryMobileTerminal.close();await open();
      assert.equal(q('#terminal-output').scrollTop,bottom(q('#terminal-output')));
    """)


def test_failed_preview_and_recovery_show_the_fresh_tail():
    run("""
      q('#terminal-output').scrollTop=100;failPreview=true;await tick();
      assert.equal(q('#terminal-output').textContent,'');
      assert.equal(q('.terminal-age').textContent,'');assert.equal(q('.terminal-preview-note').textContent,'');
      failPreview=false;const retry=[...timers].find(([,t])=>t.ms===6000);assert.ok(retry);
      timers.delete(retry[0]);await retry[1].fn();await settle();
      assert.equal(q('#terminal-output').scrollTop,bottom(q('#terminal-output')));
    """)


def test_held_text_and_its_timestamp_and_notes_stay_on_the_same_snapshot():
    run("""
      const p=q('#terminal-output'),shown=p.textContent,age=q('.terminal-age').textContent;
      p.scrollTop=100;updates=10;shift=100;truncated=true;controls=true;await tick();
      assert.equal(p.textContent,shown);assert.equal(q('.terminal-age').textContent,age);
      assert.equal(q('.terminal-age').textContent.startsWith('Shown '),true);
      assert.ok(!q('.terminal-preview-note').textContent.includes('Control sequences removed'));
      assert.equal(q('.terminal-stream').textContent,'New output · paused');
      assert.match(q('.terminal-preview-note').textContent,/Scroll to bottom/);
      assert.match(q('.terminal-preview-note').textContent,/40 lines.*6 KiB.*may be missing/);
      const field=q('#terminal-text');field.value='literal test input';field.oninput();
      await button('Review input').onclick();await settle();
      assert.equal(q('.terminal-age').textContent,age);assert.equal(q('#terminal-output').textContent,shown);
      button('Edit').onclick();await settle();const current=q('#terminal-output');
      current.scrollTop=bottom(current);current.events.scroll();
      assert.notEqual(current.textContent,shown);assert.notEqual(q('.terminal-age').textContent,age);
      assert.match(q('.terminal-preview-note').textContent,/Tail shortened to 6 KiB/);
      assert.match(q('.terminal-preview-note').textContent,/Control sequences removed/);
      assert.ok(!q('.terminal-preview-note').textContent.includes('may be missing'));
      assert.ok(current.textContent.startsWith('line 100\\n'));
      assert.ok(!current.textContent.includes('line 40\\n'),'intervening lines are not a retained history');
    """)


@pytest.mark.parametrize('reading', [False, True])
def test_keyboard_and_rotation_geometry_changes_keep_follow_or_reading_intent(reading):
    run("""
      const p=q('#terminal-output');READ;
      const shown=p.textContent;
      for(const [height,width,row]of [[112,350,17],[218,350,20.35],[150,750,20.35]]){
        viewHeight=height;viewWidth=width;rowHeight=row;updates++;shift+=3;
        await tick();
        if(READING){assert.equal(p.scrollTop,100);assert.equal(p.textContent,shown);}
        else{assert.equal(p.scrollTop,bottom(p));assert.ok(p.textContent.startsWith('line '+shift+'\\n'));}
      }
    """.replace('READ;', 'p.scrollTop=100;p.events.scroll();' if reading else '')
         .replace('READING', 'true' if reading else 'false'))


def test_input_review_send_keeps_the_exact_text_and_sends_once_while_output_is_held():
    run("""
      const p=q('#terminal-output');p.scrollTop=100;updates++;shift+=3;await tick();
      const field=q('#terminal-text');field.value='literal selected input';field.oninput();
      await button('Review input').onclick();await settle();
      assert.equal(q('.terminal-exact').textContent,'literal selected input');
      assert.equal(calls.filter(c=>c.op==='commit').length,0);
      await button('Send once').onclick();await settle();
      assert.equal(calls.filter(c=>c.op==='commit').length,1);
      assert.equal(calls.find(c=>c.op==='prepare').payload.text,'literal selected input');
      assert.equal(q('.terminal-result').textContent,'Received by terminal');
      assert.equal(q('#terminal-output').scrollTop,100);
    """)


def test_backgrounding_clears_held_output_and_draft_and_ignores_late_preview():
    run("""
      const p=q('#terminal-output');p.scrollTop=100;updates++;await tick();
      const field=q('#terminal-text');field.value='private fixture draft';field.oninput();
      holdPreview=true;const pending=tick();await settle();assert.equal(typeof releasePreview,'function');
      doc.hidden=true;doc.events.visibilitychange();
      assert.equal(q('#terminal-sheet').open,false);assert.equal(q('#terminal-sheet').children.length,0);
      releasePreview();await pending;assert.equal(q('#terminal-output'),null);
      assert.equal([...timers.values()].filter(t=>t.ms===3000||t.ms===6000).length,0);
      doc.hidden=false;holdPreview=false;await open();
      assert.equal(q('#terminal-text').value,'');
      assert.equal(q('#terminal-output').scrollTop,bottom(q('#terminal-output')));
    """)


@pytest.mark.parametrize('rerender', [False, True])
def test_successful_empty_preview_is_held_until_the_reader_returns_to_bottom(rerender):
    run("""
      const p=q('#terminal-output'),shown=p.textContent,age=q('.terminal-age').textContent;
      p.scrollTop=100;emptyPreview=true;updates++;await tick();
      assert.equal(p.textContent,shown);assert.equal(p.scrollTop,100);assert.equal(q('.terminal-age').textContent,age);
      assert.equal(q('.terminal-stream').textContent,'New output · paused');
      RERENDER;
      const current=q('#terminal-output');assert.equal(current.textContent,shown);assert.equal(current.scrollTop,100);
      assert.equal(q('.terminal-age').textContent,age);
      current.scrollTop=bottom(current);current.events.scroll();
      assert.equal(current.textContent,'');assert.equal(current.scrollTop,0);
      assert.notEqual(q('.terminal-age').textContent,age);
      assert.equal(q('.terminal-stream').textContent,'Updating every 3s');
    """.replace('RERENDER;', "const field=q('#terminal-text');field.value='safe fixture input';field.oninput();await button('Review input').onclick();await settle();button('Edit').onclick();await settle();" if rerender else ''))


def test_resize_clamp_cannot_resume_a_reader_on_the_next_scroll_or_poll():
    run("""
      let p=q('#terminal-output');viewHeight=218;rowHeight=20.35;await tick();
      p.scrollTop=bottom(p)-20;p.events.scroll();const shown=p.textContent,age=q('.terminal-age').textContent;
      viewHeight=112;rowHeight=17;p.scrollTop=p.scrollTop;p.events.scroll();
      assert.equal(p.scrollTop,bottom(p),'browser clamp puts the DOM at bottom');
      for(let i=0;i<3;i++){
        updates++;shift+=3;await tick();p.events.scroll();
        assert.equal(p.textContent,shown);assert.equal(q('.terminal-age').textContent,age);
      }
      assert.equal(q('.terminal-stream').textContent,'New output · paused');
      const field=q('#terminal-text');field.value='safe fixture input';field.oninput();
      await button('Review input').onclick();await settle();button('Edit').onclick();await settle();
      p=q('#terminal-output');assert.equal(p.textContent,shown);assert.equal(q('.terminal-age').textContent,age);
      assert.equal(typeof p.events.touchmove,'function');
      p.events.touchstart?.({touches:[{identifier:1,clientX:100,clientY:200}]});
      p.events.touchmove({touches:[{identifier:1,clientX:100,clientY:100}]});flushFrames();
      assert.notEqual(p.textContent,shown);assert.equal(p.scrollTop,bottom(p));
      assert.equal(q('.terminal-stream').textContent,'Updating every 3s');
    """)


@pytest.mark.parametrize('gesture', [
    "p.events.wheel({deltaX:100,deltaY:0})",
    "p.events.wheel({deltaX:0,deltaY:-100})",
    "p.events.wheel({deltaX:0,deltaY:100,ctrlKey:true})",
    "p.events.wheel({deltaX:0,deltaY:100,shiftKey:true})",
    "p.events.touchmove({touches:[{identifier:1,clientX:200,clientY:200}]})",
    "p.events.touchmove({touches:[{identifier:1,clientX:100,clientY:300}]})",
    "p.events.touchmove({touches:[{identifier:1,clientX:200,clientY:190}]})",
    "p.events.keydown({key:'ArrowUp'})",
    "p.events.keydown({key:'PageUp'})",
    "p.events.keydown({key:'Home'})",
    "p.events.keydown({key:' ',shiftKey:true})",
])
def test_non_downward_gestures_do_not_release_a_resize_clamp(gesture):
    run("""
      const p=q('#terminal-output');viewHeight=218;rowHeight=20.35;await tick();
      p.scrollTop=bottom(p)-20;p.events.scroll();const shown=p.textContent;
      viewHeight=112;rowHeight=17;p.scrollTop=p.scrollTop;p.events.scroll();
      updates++;shift+=3;await tick();
      p.events.touchstart?.({touches:[{identifier:1,clientX:100,clientY:200}]});
      GESTURE;flushFrames();await tick();
      assert.equal(p.textContent,shown);assert.equal(p._scrollView.following,false);
      assert.equal(q('.terminal-stream').textContent,'New output · paused');
    """.replace('GESTURE', gesture))


@pytest.mark.parametrize('gesture', [
    "p.events.wheel({deltaX:0,deltaY:100})",
    "p.events.touchmove({touches:[{identifier:1,clientX:110,clientY:100}]})",
    "p.events.keydown({key:'ArrowDown'})",
    "p.events.keydown({key:'PageDown'})",
    "p.events.keydown({key:'End'})",
    "p.events.keydown({key:' '})",
])
def test_downward_gestures_release_a_resize_clamp_on_the_next_frame(gesture):
    run("""
      const p=q('#terminal-output');viewHeight=218;rowHeight=20.35;await tick();
      p.scrollTop=bottom(p)-20;p.events.scroll();const shown=p.textContent;
      viewHeight=112;rowHeight=17;p.scrollTop=p.scrollTop;p.events.scroll();
      updates++;shift+=3;await tick();
      p.events.touchstart?.({touches:[{identifier:1,clientX:100,clientY:200}]});
      GESTURE;assert.equal(p.textContent,shown,'gesture must wait for native default scrolling');
      flushFrames();assert.notEqual(p.textContent,shown);assert.equal(p._scrollView.following,true);
    """.replace('GESTURE', gesture))


def test_gesture_frame_samples_the_position_after_native_default_scrolling():
    run("""
      const p=q('#terminal-output');p.scrollTop=100;p.events.scroll();const shown=p.textContent;
      updates++;shift+=3;await tick();
      p.events.wheel({deltaX:0,deltaY:1000});
      assert.equal(p.textContent,shown);assert.equal(frames.length,1);
      p.scrollTop=bottom(p); // browser default scroll, before its event/frame
      flushFrames();assert.notEqual(p.textContent,shown);assert.equal(p._scrollView.following,true);
    """)


@pytest.mark.parametrize('sequence,release', [
    ('start(200);move(190);move(210);', False),
    ('start(200);move(190);move(210);move(200);', True),
    ('start(200);move(190);p.events.touchstart({touches:[point(190),{identifier:2,clientX:130,clientY:190}]});', False),
    ('start(200);move(190);p.events.touchcancel();', False),
    ('p.events.wheel({deltaY:120});p.events.wheel({deltaY:-120});', False),
    ('p.events.wheel({deltaY:120});p.events.wheel({deltaX:120,deltaY:0});', False),
    ("p.events.wheel({deltaY:120});p.events.keydown({key:'ArrowUp'});", False),
])
def test_one_frame_uses_only_the_latest_input_direction(sequence, release):
    run("""
      const p=q('#terminal-output');viewHeight=218;rowHeight=20.35;await tick();
      p.scrollTop=bottom(p)-20;p.events.scroll();const shown=p.textContent,age=q('.terminal-age').textContent;
      viewHeight=112;rowHeight=17;p.scrollTop=p.scrollTop;p.events.scroll();
      updates++;shift+=3;await tick();
      const point=y=>({identifier:1,clientX:100,clientY:y});
      const start=y=>p.events.touchstart({touches:[point(y)]});
      const move=y=>p.events.touchmove({touches:[point(y)]});
      SEQUENCE;
      assert.equal(p.textContent,shown);assert.equal(frames.length,1,'only one pending input decision per frame');
      flushFrames();
      if(RELEASE){assert.notEqual(p.textContent,shown);assert.equal(p._scrollView.following,true);}
      else{assert.equal(p.textContent,shown);assert.equal(q('.terminal-age').textContent,age);assert.equal(p._scrollView.following,false);}
    """.replace('SEQUENCE', sequence).replace('RELEASE', 'true' if release else 'false'))


def test_downward_intent_does_nothing_if_native_scroll_is_not_at_bottom_at_the_frame():
    run("""
      const p=q('#terminal-output');p.scrollTop=100;p.events.scroll();const shown=p.textContent;
      updates++;shift+=3;await tick();
      const state=p._scrollView;p.events.wheel({deltaY:20});p.scrollTop=120;
      flushFrames();assert.equal(p.textContent,shown);assert.equal(p._scrollView,state);
    """)
