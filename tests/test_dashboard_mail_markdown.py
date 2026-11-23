"""The PC Mail drawer uses the bundled conversation renderer for full bodies."""
import json
from pathlib import Path

from mobile_reader_browser import MAIL_MARKDOWN, assert_mail_markdown
from test_dashboard_help_map import demo_page  # noqa: F401

DASHBOARD = Path(__file__).resolve().parents[1] / 'dashboard'


def test_network_edge_mail_renders_document_on_expand(demo_page):
    _, js, _, open_page = demo_page
    open_page('lang=en')
    message = dict(sender='SampleAgent', recipient='OtherAgent', subject='Fixture <subject>',
                   body=MAIL_MARKDOWN, importance='high', ts_unix=1767254400)
    js("""(() => {
        const original=window.fetch;
        window.fetch=(url,...options)=>String(url).startsWith('/api/edge-messages?')
            ?Promise.resolve(Response.json({ok:true,count:1,messages:[MESSAGE]}))
            :original(url,...options);
        setView('net');
    })()""".replace('MESSAGE', json.dumps(message)))
    js("openDrawer('SampleAgent','OtherAgent')")
    assert js("document.querySelector('#ed-list .ed-subj').textContent==='Fixture <subject>'")
    assert js("document.querySelector('#ed-list .ed-prev').textContent.includes('## Release checks')")
    js("document.querySelector('#ed-list .ed-subj').click()")
    assert_mail_markdown(js, '#ed-list .mail-markdown')
    js("""(() => {
        const a=document.querySelector('#ed-list .mail-markdown a');
        a.addEventListener('click',event=>event.preventDefault(),{once:true});a.click();
    })()""")
    assert js("document.querySelector('#ed-list .ed-msg').classList.contains('expanded')")
    js("document.querySelector('#ed-list .ed-subj').click()")
    assert js("document.querySelector('#ed-list .mail-markdown').hidden")


def test_isolated_reply_view_renders_selected_mail_only(demo_page):
    _, js, _, open_page = demo_page
    open_page('lang=en')
    js((DASHBOARD / 'mail_flow.js').read_text())
    items = [
        dict(id=i, sender=dict(name='SampleAgent'), to=[dict(name='OtherAgent')], cc=[], bcc=[],
             subject='Fixture subject', created_ts='2026-01-01T08:00:00Z', body_md=MAIL_MARKDOWN)
        for i in (1, 2)
    ]
    js("""(() => {
        document.getElementById('edrawer').classList.add('on');
        OrreryMailFlow.render(document.getElementById('ed-list'),{messages:ITEMS,selected:2});
    })()""".replace('ITEMS', json.dumps(items)))
    assert js("document.querySelector('#reply-1 .reply-body').textContent.includes('## Release checks')")
    assert js("document.querySelector('#reply-1 table') === null")
    assert_mail_markdown(js, '#reply-2 .reply-body')
