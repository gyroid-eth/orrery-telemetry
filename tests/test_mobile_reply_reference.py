"""Recorded references in the real mobile Reader, with synthetic pages."""
from urllib.parse import parse_qs, urlsplit

import pytest

from mobile_reader_browser import phone  # noqa: F401


def event(id, text, role='assistant', **fields):
    return dict(id=id, kind='text', role=role, text=text, ts='2026-11-24T00:40:00Z', **fields)


def reply(target, quote='The signed package is ready.', body='Please rerun the final check.',
          ts='2026-11-24T00:35:14.342Z'):
    header = 'Reference: SampleAgent · assistant' + (f' · {ts}' if ts else '')
    quoted = '\n'.join('> ' + line for line in quote.split('\n'))
    return f'{header}\nRecorded message: {target}\n{quoted}\n\n{body}'


def publish(phone, *events):
    _, js, wait, state = phone
    state['events'].extend(events)
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait(f"!!document.querySelector('[data-id=\"{events[-1]['id']}\"]')")


CARD = 'document.querySelector("[data-id=reply] .conversation-reply-reference")'


def test_recorded_reply_separates_literal_quote_and_preserves_body(phone):
    _, js, _, _ = phone
    quote = 'Check <img src=/fixture-pixel onerror=alert(1)>\n\nThen review the trace.'
    body = '  Reply to https://example.test/report\nwith the new result.  '
    publish(phone, event('reply', reply('result', quote, body).replace('\n', '\r\n'), 'user'),
            event('no-clock', reply('result', ts=''), 'user'))
    observed = js(f"""(()=>{{const card={CARD},bubble=card.closest('[data-id]'),
      quote=card.querySelector('.conversation-reply-reference-quote'),body=bubble.querySelector('.conversation-text');
      return {{quote:quote.textContent,title:card.title,label:card.getAttribute('aria-label'),
        clock:card.querySelector('time').textContent,body:body.textContent,link:body.querySelector('a').href,
        literal:quote.childElementCount===0,ordered:card.nextElementSibling===body,
        lines:getComputedStyle(quote).webkitLineClamp,
        optionalClock:!!document.querySelector('[data-id=no-clock] .conversation-reply-reference time')}};
    }})()""")
    assert observed == dict(quote=quote, title=quote, label='Jump to quoted message from SampleAgent at 09:35 JST',
                            clock='09:35', body=body, link='https://example.test/report', literal=True,
                            ordered=True, lines='3', optionalClock=False)


def test_incomplete_or_non_user_replies_stay_literal(phone):
    _, js, _, _ = phone
    valid = reply('result')
    invalid = [valid.replace('Recorded message: result\n', ''), valid.replace('result\n>', 'has spaces\n>'),
               valid.replace('\n> ', '\n>'), valid.replace('\n\n', '\n'),
               valid.replace('2026-11-24T00:35:14.342Z', 'not a time'), 'Leading text\n'+valid,
               reply('result', quote=' '), reply('result', body=' \n'), valid.rsplit('\n', 2)[0],
               'Reference: SampleAgent · assistant · 2026-11-24T00:35:14Z | Recorded message: result | Quote: "sample" | Reply: answer']
    events = [event(f'bad-{i}', text, 'user') for i, text in enumerate(invalid)]
    events += [event('bad-assistant', valid), event('bad-truncated', valid, 'user', truncated=True)]
    publish(phone, *events)
    assert js('document.querySelectorAll("[data-id^=bad-] .conversation-reply-reference").length') == 0
    assert js("document.querySelector('[data-id=bad-truncated] .conversation-text').textContent") == valid
    assert js("document.querySelector('[data-id=bad-0] .conversation-text').textContent") == invalid[0]


def test_reference_scrolls_to_loaded_message_and_highlight_expires(phone):
    _, js, wait, state = phone
    body = 'A later diagnostic reply with enough lines to put the original result above the viewport.\n' * 5
    publish(phone, *(event(f'later-{i}', body) for i in range(10)), event('reply', reply('result'), 'user'))
    before = js("(()=>{const list=document.querySelector('.conversation-list');list.scrollTop=list.scrollHeight;return list.scrollTop})()")
    assert before > 500
    assert js(CARD + ".getAttribute('aria-disabled')") == 'false'
    js(CARD + '.focus();document.activeElement.click()')
    assert js("document.activeElement.dataset.id") == 'result'
    assert js("document.querySelector('.conversation-reference-target').dataset.id") == 'result'
    assert js("document.querySelector('.conversation-list').scrollTop") < before
    assert not any('page=' in item for item in state['get_calls'])
    wait("!document.querySelector('.conversation-reference-target')", 4)


def test_reference_reads_older_pages_until_it_reaches_the_source(phone):
    _, js, wait, state = phone
    first, second, unused = (char * 32 for char in 'abc')
    state['older'] = first
    state['conversation_pages'] = {
        first: dict(events=[event('middle', 'Earlier diagnostic notes.')], older=second),
        second: dict(events=[event('original', 'The signed package is ready.')], older=unused),
    }
    publish(phone, event('reply', reply('original'), 'user'))
    assert js(CARD + ".getAttribute('aria-disabled')") == 'false'
    js(CARD + '.click()')
    wait("document.querySelector('.conversation-reference-target')?.dataset.id==='original'")
    assert js("document.activeElement.dataset.id") == 'original'
    assert [parse_qs(urlsplit(url).query)['page'][0] for url in state['get_calls'] if 'page=' in url] == [first, second]


@pytest.mark.parametrize('older', [False, True])
def test_reference_outside_loaded_record_reports_unavailable(phone, older):
    _, js, wait, state = phone
    if older:
        token = 'd' * 32
        state['older'] = token
        state['conversation_pages'] = {token: dict(events=[event('unrelated', 'An older note.')], older=None)}
    publish(phone, event('reply', reply('missing'), 'user'))
    if older:
        js(CARD + '.click()')
    wait(CARD + ".getAttribute('aria-disabled')==='true'")
    assert js(CARD + '.title').endswith('not in the loaded part of this conversation.')
    before = sum('page=' in path for path in state['get_calls'])
    assert before == int(older)
    assert js(f"""(()=>{{const list=document.querySelector('.conversation-list'),top=list.scrollTop;
      {CARD}.click();return list.scrollTop===top&&!list.querySelector('.conversation-reference-target');}})()""")
    assert sum('page=' in path for path in state['get_calls']) == before
