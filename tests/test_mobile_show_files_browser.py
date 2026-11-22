"""実ブラウザの会話内画像、既存chip、拡大、遅い応答の破棄を確認する。"""
import base64
import json
import io
import os
from pathlib import Path
import time

import pytest

from mobile_reader_browser import ROOT, phone  # noqa: F401
from test_mobile_preview import image, png


def shown(state):
    from PIL import Image, ImageDraw
    chart=Image.new('RGBA',(640,320),'#f0eee7')
    draw=ImageDraw.Draw(chart)
    for x in range(40,610,80):draw.line((x,25,x,280),fill='#d1cbbf')
    for y in range(40,281,60):draw.line((40,y,610,y),fill='#d1cbbf')
    for x,h,label in [(110,80,'512'),(280,160,'4K'),(450,230,'16K')]:
        draw.rectangle((x,280-h,x+65,280),fill='#6a8899')
        draw.text((x+15,290),label,fill='#263038')
    data=io.BytesIO();chart.save(data,format='PNG')
    state['file_preview']=image(data.getvalue(),width=640,height=320)
    state['events']=[
        dict(id='before',kind='text',role='assistant',text='比較の画像を表示します。',ts='2026-01-01T08:45:00Z'),
        dict(id='shown',kind='tool',role='assistant',text='mcp__orrery_mail__show_files',shown=True,ts='2026-01-01T08:45:30Z',files=[
            dict(label='chart.png',kind='image',ref='b'*32),dict(label='notes.md',kind='file',ref='c'*32)]),
        dict(id='after',kind='text',role='assistant',text='画像をタップすると拡大します。',ts='2026-01-01T08:46:00Z')]


@pytest.mark.parametrize('theme',['dark','light'])
def test_shown_image_is_outside_folded_tools_and_opens_existing_preview(phone,theme):
    client,js,wait,state=phone
    shown(state)
    js('OrreryMobileTerminal.open("SampleAgent")')
    wait("document.querySelector('.conversation-thumbnail-image')?.naturalWidth===320",8)
    assert js("document.querySelector('[data-id=shown]').hidden")
    assert js("document.querySelector('[data-shown-for=shown]').hidden===false")
    assert js("document.querySelector('[data-id=shown] .previewable')!==null")
    assert js("document.querySelector('[data-shown-for=shown]').innerText.includes('notes.md')")
    assert js("document.querySelector('[data-shown-for=shown] .conversation-chip').tagName")== 'SPAN'
    assert any('thumbnail=1' in path for path in state['get_calls'])
    js((ROOT/'dashboard/theme_core.js').read_text())
    js((ROOT/'dashboard/theme_controller.js').read_text())
    js("AgentStackColorTheme.apply({preference:"+json.dumps(theme)+",resolved:"+json.dumps(theme)+"})")
    out=os.environ.get('ORRERY_SHOW_FILES_SCREENSHOTS')
    if out:
        Path(out).mkdir(parents=True,exist_ok=True)
        Path(out,f'{theme}-inline.png').write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))
    js("document.querySelector('.conversation-thumbnail').focus()")
    if theme=='light':
        client.call('Input.dispatchKeyEvent',type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13)
        client.call('Input.dispatchKeyEvent',type='char',text='\r',unmodifiedText='\r',key='Enter',windowsVirtualKeyCode=13)
        client.call('Input.dispatchKeyEvent',type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13)
    else:js("document.querySelector('.conversation-thumbnail').click()")
    wait("document.querySelector('.conversation-preview-image')?.naturalWidth===640")
    if out:Path(out,f'{theme}-expanded.png').write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))
    assert any('/view/conversation/preview?' in p and 'thumbnail' not in p for p in state['get_calls'])
    if theme=='light':
        client.call('Input.dispatchKeyEvent',type='keyDown',key='Escape',code='Escape',windowsVirtualKeyCode=27)
        client.call('Input.dispatchKeyEvent',type='keyUp',key='Escape',code='Escape',windowsVirtualKeyCode=27)
    else:js("document.querySelector('.conversation-preview-close').click()")
    wait("document.querySelector('.conversation-preview')===null")
    assert js("document.activeElement.classList.contains('conversation-thumbnail')")
    before=len([p for p in state['get_calls'] if 'thumbnail=1' in p])
    time.sleep(5.2)
    assert len([p for p in state['get_calls'] if 'thumbnail=1' in p])==before
    js("document.querySelector('.conversation-tool-group').click()")
    assert js("document.querySelector('[data-id=shown]').hidden===false")


def test_thumbnail_background_retry_and_close_ignore_late_response(phone):
    client,js,wait,state=phone
    shown(state)
    js("window.realFetch=fetch;window.thumbReleases=[];window.fetch=async(...a)=>{const r=await realFetch(...a);if(String(a[0]).includes('thumbnail=1'))await new Promise(resolve=>thumbReleases.push(resolve));return r};OrreryMobileTerminal.open('SampleAgent')")
    wait('thumbReleases.length===1',8)
    js("window.isHidden=true;Object.defineProperty(document,'hidden',{configurable:true,get:()=>isHidden});document.dispatchEvent(new Event('visibilitychange'));isHidden=false;document.dispatchEvent(new Event('visibilitychange'));thumbReleases[0]()")
    wait('thumbReleases.length===2')
    js("window.oldImage=document.querySelector('.conversation-thumbnail-image');OrreryMobileTerminal.close();thumbReleases.forEach(f=>f())")
    wait("document.querySelector('.conversation-thumbnail-image')===null")
    assert js("oldImage.getAttribute('src')")==''


def test_ordinary_paths_and_ungranted_images_do_not_auto_load(phone):
    client,js,wait,state=phone
    state['events']=[dict(id='plain',kind='text',role='assistant',text='/private/example/not-shown.png',ts=''),
        dict(id='read',kind='tool',role='assistant',text='Read',ts='',files=[dict(label='read.png',kind='image',ref='b'*32)]),
        dict(id='ungranted',kind='tool',role='assistant',text='mcp__orrery_mail__show_files',shown=True,ts='',files=[dict(label='unavailable.png',kind='image')])]
    js("OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('[data-id=plain]')!==null")
    assert js("document.querySelector('.conversation-thumbnail')===null")
    assert not any('thumbnail=1' in p for p in state['get_calls'])


def test_offscreen_thumbnail_waits_until_the_reader_scrolls_to_it(phone):
    client,js,wait,state=phone
    shown(state)
    state['events']=state['events'][1:2]+[
        dict(id='text'+str(i),kind='text',role='assistant',text='続きの記録です。\n'*3,ts='') for i in range(30)]
    js("OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('[data-id=text29]')!==null")
    assert not any('thumbnail=1' in p for p in state['get_calls'])
    js("document.querySelector('.conversation-list').scrollTop=0")
    wait("document.querySelector('.conversation-thumbnail-image')?.naturalWidth===320")
