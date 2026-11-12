"""Multiple phone selections still produce one explicitly confirmed input."""
import base64
import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from mobile_reader_browser import phone


def start(js, wait, state):
    state['images'] = True
    state['image_paths'] = ['/workspace/.orrery-input-images/'+format(i, '032x')+'.png' for i in range(1, 21)]
    js("""OrreryMobileTerminal.close();
    window.attachmentURLs=[];window.revokedURLs=[];
    window.originalCreateURL=URL.createObjectURL.bind(URL);window.originalRevokeURL=URL.revokeObjectURL.bind(URL);
    URL.createObjectURL=value=>{const url=originalCreateURL(value);attachmentURLs.push(url);return url};
    URL.revokeObjectURL=url=>{revokedURLs.push(url);originalRevokeURL(url)};
    OrreryMobileTerminal.open('SampleAgent')""")
    wait("document.querySelector('#terminal-image-add')?.hidden===false")
    assert js("document.querySelector('#terminal-image-file').multiple")
    js("document.querySelector('#terminal-text').value='Compare these';document.querySelector('#terminal-text').dispatchEvent(new Event('input'))")


def select(js, count, invalid=False):
    images = []
    for i in range(count):
        image = Image.new('RGB', (240, 120), ['#c66b48', '#4f8292', '#697e52'][i % 3])
        ImageDraw.Draw(image).text((90, 50), 'Image '+str(i+1), fill='white')
        out = io.BytesIO()
        image.save(out, 'PNG')
        images.append(base64.b64encode(out.getvalue()).decode())
    js("""(()=>{
      const files=new DataTransfer(),images=DATA;
      images.forEach((data,index)=>files.items.add(new File([Uint8Array.from(atob(data),c=>c.charCodeAt(0))],'image-'+index+'.png',{type:'image/png'})));
      if(INVALID)files.items.add(new File(['invalid'],'unsupported.heic',{type:'image/heic'}));
      const input=document.querySelector('#terminal-image-file');input.files=files.files;input.dispatchEvent(new Event('change'));
    })()""".replace('DATA', json.dumps(images)).replace('INVALID', json.dumps(invalid)))


def uploads(state):
    return [body for op, body in state['calls'] if op == 'image' and body.get('image') is not None]


def capture(client, theme, step):
    folder = os.environ.get('MOBILE_MULTI_IMAGE_CAPTURE_DIR')
    if folder:
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        (path/(theme+'-'+step+'.png')).write_bytes(base64.b64decode(client.call('Page.captureScreenshot', format='png')['data']))


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_multiple_selection_add_remove_and_one_confirmed_send(phone, theme):
    client, js, wait, state = phone
    if theme == 'light':
        js((Path(__file__).resolve().parents[1]/'dashboard/theme_core.js').read_text())
        js("document.documentElement.dataset.colorTheme='light';Object.entries(AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables).forEach(([k,v])=>document.documentElement.style.setProperty(k,v))")
    start(js, wait, state)
    select(js, 3)
    wait("document.querySelectorAll('.reader-image-thumb').length===3&&!document.querySelector('#terminal-image-add').disabled")
    assert len(uploads(state)) == 3
    assert not any(op in {'prepare', 'commit'} for op, _ in state['calls'])
    assert js("document.querySelector('.reader-images').getBoundingClientRect().height<=198")
    wait("[...document.querySelectorAll('.reader-image-thumb')].every(image=>image.naturalWidth===240)")
    js("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
    capture(client, theme, 'three')
    select(js, 1)
    wait("document.querySelectorAll('.reader-image-thumb').length===4&&!document.querySelector('#terminal-image-add').disabled")
    js("document.querySelectorAll('.reader-image-remove')[1].click()")
    assert js("document.querySelectorAll('.reader-image-thumb').length") == 3
    assert js("document.querySelector('#terminal-text').value") == 'Compare these'
    assert js("revokedURLs.length") == 1
    paths = [state['image_paths'][i] for i in (0, 2, 3)]
    expected = 'Compare these '+ ' '.join("[image: '"+path+"']" for path in paths)
    assert js("document.querySelector('.reader-input-count').textContent") == str(len(expected.encode()))+' / 4096 bytes'
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.reader-review')!==null")
    assert js("document.querySelector('.terminal-exact').textContent") == expected
    assert js("document.querySelectorAll('.reader-image-thumb').length") == 3
    capture(client, theme, 'review')
    js("[...document.querySelectorAll('button')].find(button=>button.textContent==='Edit').click()")
    assert js("document.querySelector('#terminal-text').value") == 'Compare these'
    assert js("document.querySelectorAll('.reader-image-thumb').length") == 3
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.reader-review')!==null")
    js("[...document.querySelectorAll('button')].find(button=>button.textContent==='Send once').click()")
    wait("document.querySelector('.reader-receipt.accepted')!==null")
    assert state['draft'] == expected
    assert sum(op == 'commit' for op, _ in state['calls']) == 1
    assert js("attachmentURLs.every(url=>revokedURLs.includes(url))")
    assert js("document.querySelectorAll('.reader-image-thumb').length") == 0


def test_limit_and_invalid_batch_preserve_existing_selection(phone):
    _, js, wait, state = phone
    start(js, wait, state)
    select(js, 1, invalid=True)
    wait("document.querySelector('.terminal-message').textContent.includes('JPEG')")
    assert not uploads(state)
    select(js, 10)
    wait("document.querySelectorAll('.reader-image-thumb').length===10")
    wait("document.querySelector('.terminal-message').textContent===''", 10)
    assert js("document.querySelector('#terminal-image-add').disabled")
    select(js, 1)
    wait("document.querySelector('.terminal-message').textContent.includes('10 images')")
    assert len(uploads(state)) == 10
    assert js("document.querySelectorAll('.reader-image-thumb').length") == 10
    js("document.querySelector('.reader-image-remove').click()")
    assert not js("document.querySelector('#terminal-image-add').disabled")
    assert js("document.querySelector('#terminal-text').value") == 'Compare these'
    assert not any(op in {'prepare', 'commit'} for op, _ in state['calls'])


def test_failure_in_second_image_stops_batch_and_retains_first(phone):
    _, js, wait, state = phone
    start(js, wait, state)
    state.update(image_error='image_folder_full', image_error_at=2)
    select(js, 3)
    wait("document.querySelector('.terminal-message').textContent.includes('50 images')")
    assert len(uploads(state)) == 2
    assert js("document.querySelectorAll('.reader-image-thumb').length") == 1
    assert js("document.querySelector('#terminal-text').value") == 'Compare these'
    assert not any(op in {'prepare', 'commit'} for op, _ in state['calls'])
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.reader-review')!==null")
    assert state['draft'] == "Compare these [image: '"+state['image_paths'][0]+"']"


@pytest.mark.parametrize('change', ['close', 'revision'])
def test_second_upload_cannot_restore_attachments_after_target_change(phone, change):
    _, js, wait, state = phone
    start(js, wait, state)
    js("""window.originalFetch=fetch;window.releaseImage=null;window.uploadCount=0;
      window.fetch=async(...args)=>{const response=await originalFetch(...args);
      if(String(args[0]).endsWith('/image')&&JSON.parse(args[1].body).image!==null&&++uploadCount===2)
        return new Promise(resolve=>releaseImage=()=>resolve(response));return response}""")
    select(js, 3)
    wait("releaseImage!==null")
    assert js("attachmentURLs.length") == 1
    if change == 'close':
        js("OrreryMobileTerminal.close()")
    else:
        state['targets'] = [dict(choice='fixture-choice', name='SampleAgent', pane_label='Window 0 · pane 0', revision='changed-revision')]
        wait("document.querySelector('.terminal-message').textContent.includes('Pane changed')", 8)
    js("releaseImage();new Promise(resolve=>setTimeout(resolve,100))")
    assert len(uploads(state)) == 2
    assert js("attachmentURLs.every(url=>revokedURLs.includes(url))")
    assert js("document.querySelectorAll('.reader-image-thumb').length") == 0
    assert not any(op in {'prepare', 'commit'} for op, _ in state['calls'])
