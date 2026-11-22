"""Reference composition and pasted line endings share one validation note."""
import pytest

from mobile_reader_browser import phone
from test_mobile_reference_reply import setup, stage, draft, capture
from test_mobile_input_newline import COMMAND


def test_reference_reply_strips_trailing_newlines_before_one_send(phone):
    client,js,wait,state=setup(phone)
    stage(client,js,wait)
    draft(js,COMMAND+'\r\n\n')
    assert js("!document.querySelector('#terminal-review').disabled")
    assert js("document.querySelector('#terminal-input-error').hidden")
    capture(client,'combined-reference-newline-ready')
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.terminal-exact')!==null")
    payload=js("document.querySelector('.terminal-exact').textContent")
    assert payload.startswith('Reference: SampleAgent · assistant · ')
    assert 'Recorded message: reference' in payload
    assert payload.endswith(' | Reply: '+COMMAND)
    assert '\n' not in payload and '\r' not in payload
    capture(client,'combined-reference-newline-review')
    assert [body['text'] for op,body in state['calls'] if op=='prepare']==[payload]
    js("const send=[...document.querySelectorAll('button')].find(b=>b.textContent==='Send once');send.click();send.click()")
    wait("document.querySelector('.reader-receipt.accepted')!==null")
    assert state['draft']==payload
    assert sum(op=='commit' for op,_ in state['calls'])==1


@pytest.mark.parametrize('text,reason',[(COMMAND+'\nsecond command','Line breaks cannot be sent'),('x'*4097,'4096 bytes')])
def test_reference_input_has_one_shared_validation_note(phone,text,reason):
    client,js,wait,state=setup(phone)
    stage(client,js,wait);draft(js,text)
    assert js("document.querySelector('#terminal-review').disabled")
    assert js("document.querySelectorAll('.terminal-input-error').length")==1
    assert reason in js("document.querySelector('#terminal-input-error').textContent")
    assert js("document.querySelector('.reader-reference-card .terminal-input-error')===null")
    assert js("document.querySelector('.reader-reference-error')===null")
    if '\n' in text:capture(client,'combined-reference-internal-newline')
    assert not any(op in {'prepare','commit'} for op,_ in state['calls'])


def test_removing_reference_retains_the_raw_reply_and_normalized_payload(phone):
    client,js,wait,state=setup(phone)
    stage(client,js,wait);draft(js,COMMAND+'\n')
    js("document.querySelector('.reader-reference-remove').click()")
    assert js("document.querySelector('#terminal-text').value")==COMMAND+'\n'
    assert js("document.querySelectorAll('.terminal-input-error').length")==1
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.terminal-exact')!==null")
    assert js("document.querySelector('.terminal-exact').textContent")==COMMAND
