"""The real phone Reader plays C, M2 and R1 with a private API fixture."""
from datetime import datetime, timezone
import json
import os

import pytest

from mobile_reader_browser import phone  # noqa: F401
from test_mobile_reference_reply import draft, setup, stage


def media(client, reduced=False):
    client.call('Emulation.setEmulatedMedia', features=[{
        'name': 'prefers-reduced-motion', 'value': 'reduce' if reduced else 'no-preference'}])


def send_once(js, wait, text):
    draft(js, text)
    js("document.querySelector('#terminal-review').click()")
    wait("!!document.querySelector('.reader-review .terminal-exact')")
    js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Send once').click()")


@pytest.mark.parametrize('reference,reduced', [(False,False),(True,False),(True,True)])
def test_c_starts_at_the_review_input_and_keeps_one_row_until_the_record(phone,reference,reduced):
    client,js,wait,state=setup(phone)
    media(client,reduced)
    if reference:
        stage(client,js,wait)
    js("""
      window.flights=[];
      const base=Element.prototype.animate;
      Element.prototype.animate=function(frames,timing){
        const effect=base.call(this,frames,timing);
        if(this.matches('.conversation-pending,[data-id=motion-record]')){
          flights.push({copy:this.textContent,transform:frames[0].transform,duration:timing.duration});
          effect.updatePlaybackRate(.02);
        }
        return effect;
      };
      const nativeFetch=window.fetch;
      window.releaseCommit=null;
      window.fetch=async(...args)=>{
        const response=await nativeFetch(...args);
        if(String(args[0]).endsWith('/commit'))return new Promise(resolve=>{releaseCommit=()=>resolve(response);});
        return response;
      };
    """)
    text='Keep this scale in the final chart.'
    send_once(js,wait,text)
    wait("releaseCommit&&!!document.querySelector('.conversation-pending[data-state=sending]')")
    js("window.pendingRow=document.querySelector('.conversation-pending')")
    assert js("pendingRow.querySelector('.conversation-text').textContent")==text
    assert js("!!pendingRow.querySelector('.conversation-reply-reference')")==reference
    assert js("!pendingRow.textContent.includes('Recorded message:')")
    assert js('flights.length')==(0 if reduced else 1)
    if not reduced:
        assert js("flights[0].duration===610&&flights[0].transform.startsWith('translate(')")
        js("pendingRow.getAnimations()[0].currentTime=120")
    js('releaseCommit()')
    wait("pendingRow.dataset.state==='accepted'")
    assert js("pendingRow===document.querySelector('.conversation-pending')")
    payload=state['draft']
    assert payload.startswith('Reference: ')==reference
    state['events'].append(dict(id='motion-record',kind='text',role='user',text=payload,ts=datetime.now(timezone.utc).isoformat()))
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("!!document.querySelector('[data-id=motion-record]')")
    assert js("!pendingRow.isConnected")
    assert js("!!document.querySelector('[data-id=motion-record] .conversation-reply-reference')")==reference
    assert js("document.querySelector('[data-id=motion-record] .conversation-text').textContent")==text
    assert [op for op,_ in state['calls']].count('commit')==1
    if not reduced:
        assert js("flights.length===2&&flights[1].duration<610"), js('flights')


@pytest.mark.parametrize('direction',['sent','received'])
def test_m2_reveals_the_route_before_the_subject(phone,direction):
    client,js,wait,state=phone
    media(client)
    demo=bool(os.environ.get('ORRERY_DEMO_BRIDGE_ROOT'))
    if demo:
        state['demo']=dict(version=1,supported=True,on=True,words=['Fixture Person'],
                           identity=dict(users=['fixture-user'],hosts=['fixture-host']))
        js('OrreryMobileDemo.refresh()')
        wait("window.OrreryDemo?.on")
    js("""
      window.routeCues=[];
      const base=Element.prototype.animate;
      Element.prototype.animate=function(frames,timing){
        if(this.closest('[data-id=motion-mail]'))
          routeCues.push([this.className,timing.delay,this.closest('.conversation-mail').textContent]);
        return base.call(this,frames,timing);
      };
    """)
    state['events'].append(dict(id='motion-mail',kind='mail',role='assistant',text='',ts=datetime.now(timezone.utc).isoformat(),
        mail=[dict(direction=direction,sender='SourceFixture',recipients=['TargetFixture'],importance='high',
                   subject='Fixture Person reviewed the chart carefully',body='The report is ready.',coverage='complete')]))
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("routeCues.some(cue=>cue[0]==='reader-mail-word')")
    effects=js('routeCues')
    assert next(delay for name,delay,_ in effects if 'conversation-mail-sender' in name)==0
    assert next(delay for name,delay,_ in effects if name=='conversation-mail-arrow')==85
    assert [delay for name,delay,_ in effects if name=='reader-mail-word']==[245,315,385,455]
    if demo:
        assert all('Fixture Person' not in copy for _,_,copy in effects)
    js("document.querySelector('[data-id=motion-mail] .conversation-mail-toggle').click()")
    assert js("!document.querySelector('[data-id=motion-mail] .conversation-mail-body').hidden")


def test_c_masks_before_motion_and_a_record_can_arrive_before_confirmation(phone):
    if not os.environ.get('ORRERY_DEMO_BRIDGE_ROOT'):
        pytest.skip('paired Demo scripts required')
    client,js,wait,state=setup(phone)
    media(client)
    stage(client,js,wait)
    state['demo']=dict(version=1,supported=True,on=True,words=['Fixture Person'],
                       identity=dict(users=['fixture-user'],hosts=['fixture-host']))
    js("OrreryMobileDemo.refresh()")
    wait("OrreryDemo?.on")
    js("""
      window.firstFlight=null;window.finishSend=null;
      const animate=Element.prototype.animate,nativeFetch=window.fetch;
      Element.prototype.animate=function(...args){
        if(this.matches('.conversation-pending')&&!firstFlight)firstFlight=this.textContent;
        return animate.apply(this,args);
      };
      window.fetch=async(...args)=>{
        const response=await nativeFetch(...args);
        if(String(args[0]).endsWith('/commit'))return new Promise(resolve=>{finishSend=()=>resolve(response);});
        return response;
      };
    """)
    send_once(js,wait,'Ask Fixture Person about the chart.')
    wait('Boolean(firstFlight&&finishSend)')
    first=js('firstFlight')
    assert 'Fixture Person' not in first and '**************' in first
    assert 'Recorded message:' not in first and 'Fixture Person' in state['draft']
    state['events'].append(dict(id='early-record',kind='text',role='user',text=state['draft'],
        ts=datetime.now(timezone.utc).isoformat()))
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("!!document.querySelector('[data-id=early-record] .conversation-reply-reference')")
    assert js("!document.querySelector('.conversation-pending')")
    js('finishSend()')
    wait("!!document.querySelector('.reader-receipt.accepted')")
    assert js("!document.querySelector('.conversation-pending')")


@pytest.mark.parametrize('reduced',[False,True])
def test_r1_reaches_every_word_with_wrapped_columns_in_screen_order(phone,reduced):
    client,js,wait,state=phone
    media(client,reduced)
    js("""
      const css=document.createElement('style');
      css.textContent='.conversation-table td{max-width:12ch;vertical-align:top}';
      document.head.append(css);
      window.wordsPlayed=[];window.initialView=null;
      const base=Element.prototype.animate;
      Element.prototype.animate=function(frames,timing){
        if(this.className==='reader-reply-word'){
          const body=this.closest('.conversation-text'),list=body.closest('.conversation-list');
          if(!initialView)initialView={
            inView:this.getBoundingClientRect().bottom<=list.getBoundingClientRect().bottom,
            tableOpacity:getComputedStyle(body.querySelector('table .reader-reply-word')).opacity};
          wordsPlayed.push([this.textContent,performance.now()]);
        }
        return base.call(this,frames,timing);
      };
    """)
    body=('First paragraph reads from the top. ' * 7 +
          '\n\n次の段落には日本語が入ります。\n\n- ListOne begins here.\n- ListTwo follows it.'
          '\n\n| Left | Right |\n| --- | --- |\n'
          '| LeftFirst LeftSecond LeftThird LeftLast | RightFirst RightSecond RightThird RightLast |')
    state['events'].append(dict(id='motion-reply',kind='text',role='assistant',text=body,ts=datetime.now(timezone.utc).isoformat()))
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("!!document.querySelector('[data-id=motion-reply]')")
    if reduced:
        assert js("wordsPlayed.length===0&&!document.querySelector('[data-reply-queued]')")
        return
    wait("!document.querySelector('[data-reply-queued]')&&wordsPlayed.length>40",10)
    samples=js('wordsPlayed')
    words=[word for word,_ in samples]
    assert js('initialView')=={'inView':True,'tableOpacity':'0'}
    assert words.index('First')<words.index('次')<words.index('ListOne')<words.index('ListTwo')<words.index('Left')
    assert words.index('LeftFirst')<words.index('RightFirst')<words.index('LeftLast')<words.index('RightLast')
    assert min(b[1]-a[1] for a,b in zip(samples,samples[1:]))>=33
