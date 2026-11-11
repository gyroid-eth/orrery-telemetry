"""表示済みファイルの変換、縮小と既存の受け付けを隔離HTTPで確認する。"""
import base64
import builtins
import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from dashboard import mobile_history as history
from test_mobile_history import OWNER, Cockpit, cockpit, event, get, reader, serving
from test_mobile_preview import COCKPIT_REF, image, png


@pytest.mark.parametrize('tool',['mcp__orrery_mail__show_files','mcp__orrery-mail__show_files'])
def test_show_files_keeps_issued_image_reference_and_marks_the_display(tool):
    raw=event(1,kind='tool',role='assistant',text=tool,references=[
        dict(id=COCKPIT_REF,label='chart.png',kind='image',path='/private/example/chart.png')])
    issued=[]
    result=history.project_event(raw,issue=lambda ref: issued.append(ref) or 'b'*32)
    assert result['shown'] is True
    assert result['files']==[dict(label='chart.png',kind='image',ref='b'*32)]
    assert issued==[COCKPIT_REF]
    assert '/private/example' not in json.dumps(result)


@pytest.mark.parametrize('tool',['Read','functions.exec','tools.mcp__orrery_mail__show_files',
                               'mcp__other__show_files','show_files','mcp__orrery_mail__show_files extra'])
def test_other_tools_or_wrapper_text_do_not_gain_a_shown_marker(tool):
    result=history.project_event(event(1,kind='tool',text=tool,shown=True,
        references=[dict(label='chart.png',kind='image')]))
    assert 'shown' not in result
    assert result['files']==[dict(label='chart.png',kind='image')]


@pytest.mark.parametrize('kind,role',[('text','assistant'),('system','system')])
def test_tool_name_in_text_or_notification_is_not_an_explicit_display(kind,role):
    result=history.project_event(event(1,kind=kind,role=role,text='mcp__orrery_mail__show_files',
        references=[dict(label='chart.png',kind='image')]))
    assert 'shown' not in result


def test_thumbnail_is_small_plain_png_and_full_preview_stays_full_size():
    from PIL import Image
    original=image(png(640,320),width=640,height=320)
    def reply(query):
        if 'ref' in query:return 200,original
        return 200,dict(ok=True,provider='codex',events=[event(1,kind='tool',text='mcp__orrery_mail__show_files',
            references=[dict(id=COCKPIT_REF,label='chart.png',kind='image')])])
    with cockpit(reply) as upstream:
        client=history.HistoryClient(upstream)
        with serving(reader(client=client)) as port:
            page=json.loads(get(port,'/view/conversation?name=Vega')[1])
            assert page['events'][0]['shown'] is True
            ref=page['events'][0]['files'][0]['ref']
            status,body=get(port,'/view/conversation/preview?name=Vega&ref='+ref+'&thumbnail=1')
            assert status==200
            small=json.loads(body)
            assert (small['width'],small['height'])==(320,160)
            data=base64.b64decode(small['data'])
            assert history.png_size(data)==(320,160)
            with Image.open(io.BytesIO(data)) as decoded:
                assert decoded.info=={}
                assert decoded.getpixel((0,0))==(16,32,48,255)
            full=json.loads(get(port,'/view/conversation/preview?name=Vega&ref='+ref)[1])
            assert full['data']==original['data']
            assert (full['width'],full['height'])==(640,320)
        for path,_ in Cockpit.requests:
            query=parse_qs(urlsplit(path).query)
            assert 'thumbnail' not in query
            if urlsplit(path).path.endswith('/preview'):assert query=={'ref':[COCKPIT_REF]}


@pytest.mark.parametrize('query',[
    'name=Vega&path=/private/example/chart.png&thumbnail=1',
    'name=Vega&ref='+'b'*32+'&path=/private/example/chart.png&thumbnail=1',
    'name=Vega&ref='+'b'*32+'&thumbnail=1',
    'name=Vega&ref='+'b'*32+'&thumbnail=2',
    'name=Vega&ref='+'b'*32+'&thumbnail=1&thumbnail=1',
])
def test_thumbnail_never_accepts_an_arbitrary_path_or_unissued_reference(query):
    # 未発行refは、実HistoryClientの発行表で拒否する。
    with cockpit(lambda _:pytest.fail('上流へ到達した')) as upstream:
        with serving(reader(client=history.HistoryClient(upstream))) as port:
            status,body=get(port,'/view/conversation/preview?'+query)
            assert status in {400,410}
            assert b'iVBOR' not in body and b'/private/example' not in body
        assert Cockpit.requests==[]


@pytest.mark.parametrize('case',['host','origin','login_missing','login_other','cross_site','disabled'])
def test_thumbnail_refusals_do_not_read_or_return_image_bytes(case):
    class Never:
        def preview(self,*args,**kwargs):pytest.fail('拒否された要求がpreviewへ到達した')
    headers=dict(OWNER)
    if case=='host':headers['Host']='outsider.example.test'
    if case=='origin':headers['Origin']='https://outsider.example.test'
    if case=='login_missing':headers.pop('Tailscale-User-Login')
    if case=='login_other':headers['Tailscale-User-Login']='other@example.test'
    if case=='cross_site':headers['Sec-Fetch-Site']='cross-site'
    with serving(reader(enabled=case!='disabled',client=Never())) as port:
        status,body=get(port,'/view/conversation/preview?name=Vega&ref='+'b'*32+'&thumbnail=1',headers)
        assert status==(404 if case=='disabled' else 403)
        assert b'iVBOR' not in body


def test_thumbnail_does_not_turn_text_or_bad_png_into_an_image():
    with pytest.raises(history.HistoryUnavailable):
        history.thumbnail_preview(dict(kind='text',text='not image'))
    with pytest.raises(history.HistoryUnavailable):
        history.project_preview(image(b'not a png'))


def test_thumbnail_without_pillow_uses_only_the_existing_checked_preview(monkeypatch):
    original=history.project_preview(image())
    native_import=builtins.__import__
    def without_pillow(name,*args,**kwargs):
        if name=='PIL':raise ImportError('試験用の未導入環境')
        return native_import(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',without_pillow)
    assert history.thumbnail_preview(original) is original
