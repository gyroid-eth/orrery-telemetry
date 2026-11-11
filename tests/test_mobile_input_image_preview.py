"""人の入力画像も、既存の参照発行と縮小表示で出す。"""
import base64
import json
import os
from pathlib import Path

import pytest

from dashboard import mobile_history as history
from test_mobile_history import cockpit, event, get, reader, serving
from test_mobile_preview import COCKPIT_REF, image, png
from mobile_reader_browser import phone


def input_event():
    return event(1,kind='text',role='user',text='この画像を確認してください。',input_images=True,
                 references=[dict(id=COCKPIT_REF,label='入力画像.png',kind='image',input_image=True)])


@pytest.mark.parametrize('kind,role,marked,expected',[
    ('text','user',True,True),('text','user',False,False),('text','assistant',True,False),('tool','assistant',True,False)])
def test_only_marked_user_input_uses_the_existing_shown_image_display(kind,role,marked,expected):
    raw=input_event();raw.update(kind=kind,role=role,input_images=marked)
    result=history.project_event(raw,issue=lambda _: 'b'*32)
    assert result.get('shown',False) is expected
    assert result['files']==[dict(label='入力画像.png',kind='image',ref='b'*32)]
    assert 'input_images' not in result and COCKPIT_REF not in json.dumps(result)


def test_user_image_preview_is_issued_and_read_via_the_existing_relay():
    def reply(query):
        if 'ref' in query:return 200,image(png(640,320),width=640,height=320)
        return 200,dict(ok=True,provider='codex',events=[input_event()])
    with cockpit(reply) as upstream:
        with serving(reader(client=history.HistoryClient(upstream))) as port:
            page=json.loads(get(port,'/view/conversation?name=Vega')[1]);shown=page['events'][0]
            assert shown['shown'] is True
            ref=shown['files'][0]['ref']
            status,body=get(port,'/view/conversation/preview?name=Vega&ref='+ref+'&thumbnail=1')
            assert status==200 and json.loads(body)['width']==320


def test_real_phone_renders_the_user_thumbnail_and_opens_the_same_image(phone):
    client,js,wait,state=phone
    state['events']=[history.project_event(input_event(),issue=lambda _: 'b'*32)]
    state['file_preview']=image(png(640,320),width=640,height=320,notice='Recorded input image')
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('.conversation-thumbnail-image')?.naturalWidth>0")
    assert js("document.querySelector('.conversation-shown-files').previousElementSibling.classList.contains('user')")
    if os.environ.get('MOBILE_INPUT_IMAGE_CAPTURE'):
        path=Path(os.environ['MOBILE_INPUT_IMAGE_CAPTURE'])
        path.with_name(path.name.replace('expanded','inline')).write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))
    js("document.querySelector('.conversation-thumbnail').click()")
    wait("document.querySelector('.conversation-preview-image')?.naturalWidth===640")
    if os.environ.get('MOBILE_INPUT_IMAGE_CAPTURE'):
        Path(os.environ['MOBILE_INPUT_IMAGE_CAPTURE']).write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))
