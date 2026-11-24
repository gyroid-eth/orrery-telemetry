"""Recorded references in the real mobile Reader, with synthetic pages."""
import json
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
        portrait:new URL(card.querySelector('.conversation-avatar img')?.src||location.href).searchParams.get('name'),
        fallback:card.querySelector('.conversation-avatar .initials')?.textContent??'',
        arrow:card.lastElementChild.textContent,body:body.textContent,link:body.querySelector('a').href,
        literal:quote.childElementCount===0,ordered:card.nextElementSibling===body.parentElement,
        whiteSpace:getComputedStyle(quote).whiteSpace,overflow:getComputedStyle(quote).textOverflow,
        hasTime:!!card.querySelector('time'),
        noClock:document.querySelector('[data-id=no-clock] .conversation-reply-reference').getAttribute('aria-label')}};
    }})()""")
    assert observed['portrait'] == 'SampleAgent' or observed['fallback'] == 'S'
    observed.pop('portrait')
    observed.pop('fallback')
    assert observed == dict(quote=quote.split('\n')[0], title='quoted message from SampleAgent at 09:35 JST\n'+quote,
                            label='Jump to quoted message from SampleAgent at 09:35 JST',
                            arrow='↗', body=body, link='https://example.test/report', literal=True, ordered=True,
                            whiteSpace='nowrap', overflow='ellipsis', hasTime=False,
                            noClock='Jump to quoted message from SampleAgent')


def test_recorded_one_line_reply_preserves_escaped_quote_and_reply_delimiter(phone):
    _, js, _, _ = phone
    quote = 'Keep "quoted text" | Reply: inside the quote.\nSecond line.'
    body = 'Check again | Reply: this is still the reply.'
    text = ('Reference: SampleAgent · assistant · 2026-11-24T00:35:14Z | Recorded message: result'
            f' | Quote: {json.dumps(quote)} | Reply: {body}')
    publish(phone, event('reply', text, 'user'))
    assert js(CARD + '.querySelector(".conversation-reply-reference-quote").textContent') == quote.splitlines()[0]
    assert js(CARD + '.title').endswith(quote)
    assert js('document.querySelector("[data-id=reply] .conversation-text").textContent') == body
    assert js(CARD + '.getAttribute("aria-disabled")') == 'false'


def test_incomplete_or_non_user_replies_stay_literal(phone):
    _, js, _, _ = phone
    valid = reply('result')
    invalid = [valid.replace('Recorded message: result\n', ''), valid.replace('result\n>', 'has spaces\n>'),
               valid.replace('\n> ', '\n>'), valid.replace('\n\n', '\n'),
               valid.replace('2026-11-24T00:35:14.342Z', 'not a time'), 'Leading text\n'+valid,
               reply('result', quote=' '), reply('result', body=' \n'), valid.rsplit('\n', 2)[0],
               'Reference: SampleAgent · assistant · 2026-11-24T00:35:14Z | Recorded message: result | Quote: "\\q" | Reply: answer']
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


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_thread_and_wash_use_the_cockpit_layout_on_a_phone(phone, theme):
    _, js, _, state = phone
    if theme == 'light':
        js("document.getElementById('mobile-theme').click()")
    publish(phone, event('first', '確認用の画像をもう一度見せてください。', 'user'),
            event('reply', reply('result', '色数を減らし、凡例が読み取れるようにします。' * 5,
                                'その配置で進めて。ラベルの字も少し大きくしてほしい。'), 'user'))
    structure = js(f"""(async()=>{{await document.fonts.ready;
      const card={CARD},wash=card.nextElementSibling,first=document.querySelector('[data-id=first]');
      const layout=node=>{{const style=getComputedStyle(node);return {{
        radius:style.borderRadius,border:style.borderRightWidth,fill:style.backgroundImage,font:style.fontFamily,
        width:node.clientWidth,right:node.getBoundingClientRect().right
      }}}};
      const q=card.querySelector('.conversation-reply-reference-quote');
      const name=card.querySelector('.conversation-reply-reference-agent');
      return {{theme:document.documentElement.dataset.colorTheme,screen:innerWidth,
        fontPx:getComputedStyle(wash.querySelector('.conversation-text')).fontSize,
        latin:document.fonts.check('400 12.5px "IBM Plex Sans"','Reader'),
        jp:document.fonts.check('400 12.5px "IBM Plex Sans JP"','確認'),
        first:layout(first),wash:layout(wash),card:layout(card),quoteTruncates:q.clientWidth<q.scrollWidth,
        nameFits:name.clientWidth>=name.scrollWidth,
        gap:wash.getBoundingClientRect().top-card.getBoundingClientRect().bottom}};
    }})()""")
    assert structure['theme'] == theme and structure['screen'] == 390
    assert structure['fontPx'] == '12.5px' and structure['latin'] and structure['jp']
    assert '/fonts/ibm-plex-sans.woff2' in state['get_calls']
    assert '/fonts/ibm-plex-sans-jp-400.woff2' in state['get_calls']
    assert structure['quoteTruncates'] and structure['nameFits'] and structure['gap'] <= 3
    for part in ['first', 'wash', 'card']:
        assert structure[part]['border'] == '0px' and structure[part]['radius'] == '0px'
    assert structure['first']['fill'].startswith('linear-gradient(270deg')
    assert structure['wash']['fill'] == structure['first']['fill']
    assert structure['card']['fill'] == 'none'
    assert structure['card']['right'] == structure['wash']['right'] == structure['first']['right']


def test_jump_remains_highlighted_briefly_with_reduced_motion(phone):
    client, js, wait, _ = phone
    client.call('Emulation.setEmulatedMedia', features=[dict(name='prefers-reduced-motion', value='reduce')])
    publish(phone, event('reply', reply('result'), 'user'))
    js(CARD + '.click()')
    assert js("getComputedStyle(document.querySelector('.conversation-reference-target')).animationName") == 'none'
    assert js("getComputedStyle(document.querySelector('.conversation-reference-target')).backgroundColor") != 'rgba(0, 0, 0, 0)'
    wait("!document.querySelector('.conversation-reference-target')", 4)
