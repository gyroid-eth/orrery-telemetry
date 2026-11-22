"""スマホの Mail の送り手表示・名前と肖像からの Reader 移動。"""
import base64
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from mobile_reader_browser import phone  # noqa: F401


def mail_event(sender='', recipients=None):
    return dict(id='followup-mail',kind='mail',role='assistant',text='',ts='',mail=[dict(
        direction='sent',sender=sender,recipients=recipients or ['RecipientAgent'],
        importance='normal',subject='記録された Mail',body='本文の記録',coverage='request',truncated=False)])


def test_sent_mail_missing_sender_has_owner_portrait_and_separate_disclosure(phone):
    client,js,wait,state=phone
    state['events'].append(mail_event())
    wait("document.querySelector('[data-id=followup-mail] .conversation-mail-sender .conversation-avatar')!==null",8)
    assert js("document.querySelector('[data-id=followup-mail] .conversation-mail-sender .conversation-mail-peer-name').textContent==='SampleAgent'")
    assert js("document.querySelector('[data-id=followup-mail] button button')===null")
    js("document.querySelector('[data-id=followup-mail] .conversation-mail-toggle').click()")
    assert js("document.querySelector('[data-id=followup-mail] .conversation-mail-body').hidden===false")
    assert js("document.querySelector('[data-id=followup-mail] .conversation-mail-recipients button').disabled")
    before=len(state['calls'])
    js("document.querySelector('[data-id=followup-mail] .conversation-mail-sender').click()")
    assert len(state['calls'])==before
    assert js("document.querySelector('[data-id=followup-mail] .conversation-mail-body').hidden===false")
    folder=os.environ.get('ORRERY_READER_FOLLOWUP_SCREENSHOTS')
    if folder:
        Path(folder).mkdir(parents=True,exist_ok=True)
        for theme in ('dark','light'):
            if theme=='light':
                root=Path(__file__).resolve().parents[1]
                js((root/'dashboard/theme_core.js').read_text())
                js("document.documentElement.dataset.colorTheme='light';Object.entries(AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables).forEach(([k,v])=>document.documentElement.style.setProperty(k,v))")
            Path(folder,theme+'-mail.png').write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))


@pytest.mark.parametrize('side,part,keyboard',[
    ('sender','name',False),('sender','mark',False),
    ('recipients','name',False),('recipients','mark',False),('sender','name',True)])
def test_mail_name_portrait_and_keyboard_open_the_selected_reader(phone,side,part,keyboard):
    client,js,wait,state=phone
    owner=state['snapshot']['agents'][0]
    state['snapshot']['agents'].extend({**owner,'name':n,'mail_instance_id':'a'*32,'mail_agent_id':i}
        for i,n in enumerate(('SenderAgent','RecipientAgent'),101))
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("OrreryMobileProfile.forName('RecipientAgent')!==null")
    event=mail_event('SenderAgent')
    if side=='sender':event['mail'][0]['direction']='received'
    state['events'].append(event)
    wait("document.querySelector('[data-id=followup-mail] .conversation-mail-recipients .conversation-avatar')!==null",8)
    target='SenderAgent' if side=='sender' else 'RecipientAgent'
    js("window.openedPeers=[];const originalOpen=OrreryMobileTerminal.open;OrreryMobileTerminal.open=(...args)=>{openedPeers.push(args);return originalOpen(...args)}")
    selector='[data-id=followup-mail] .conversation-mail-'+side+' .conversation-mail-'+('peer-name' if part=='name' else 'mark')
    if keyboard:
        js('document.querySelector('+json.dumps(selector)+').closest("button").focus()')
        client.call('Input.dispatchKeyEvent',type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13)
        client.call('Input.dispatchKeyEvent',type='char',text='\r',unmodifiedText='\r',key='Enter',windowsVirtualKeyCode=13)
        client.call('Input.dispatchKeyEvent',type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13)
    else:
        js('document.querySelector('+json.dumps(selector)+').click()')
    wait('openedPeers.length===1')
    assert js('openedPeers[0][0]')==target
    assert js('openedPeers[0][1]')=={'mail_instance_id':'a'*32,'mail_agent_id':101 if side=='sender' else 102}
    assert js('openedPeers[0][2]')=='conversation'
    assert js('document.querySelector(".reader-bar h2").textContent')==target
    wait("document.querySelector('[data-id=followup-mail]')!==null")
    assert any(parse_qs(urlsplit(path).query).get('name')==[target]
               for path in state['get_calls'] if path.startswith('/view/conversation?'))
