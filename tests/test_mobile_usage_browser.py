"""Mobile usage strip and sheet on the real Reader page."""
import time

from mobile_reader_browser import phone


def test_usage_opens_from_header_and_labels_stale_unavailable_and_reset(phone):
    client, js, wait, fixture = phone
    js('OrreryMobileTerminal.close()')
    wait("!document.getElementById('terminal-sheet').open")
    now = int(time.time())
    fixture['usage'] = dict(server_time=now, fresh_seconds=300, providers=[
        dict(provider='claude', status='available', windows=[
            dict(window_seconds=18000, remaining_percent=64, status='available',
                 observed_at=now - 20, resets_at=now + 7200),
            dict(window_seconds=604800, remaining_percent=86, status='available',
                 observed_at=now - 20, resets_at=now + 500_000),
        ]),
        dict(provider='codex', status='available', windows=[
            dict(window_seconds=604800, remaining_percent=18, status='available',
                 observed_at=now - 420, resets_at=now - 15),
        ]),
        dict(provider='antigravity', status='unavailable', windows=[]),
        dict(provider='dsh', status='available', windows=[], balance_usd='8.40',
             balance_available=True, observed_at=now - 35),
    ])
    js('location.reload()')
    wait("document.querySelectorAll('.usage-compact').length===4&&!document.getElementById('terminal-sheet').open")
    assert js('innerWidth') == 390
    assert js("document.getElementById('usage-strip').innerText.includes('Stale')")
    assert js("document.getElementById('usage-strip').getAttribute('aria-label').includes('Antigravity Unavailable')")
    assert js("Array.from(document.querySelectorAll('.usage-compact')).every(el=>el.offsetTop===document.querySelector('.usage-compact').offsetTop)")
    js("document.getElementById('usage-strip').click()")
    assert js("document.getElementById('usage-sheet').matches(':modal')")
    sheet = js("document.getElementById('usage-details').innerText")
    for value in ('5h window', '7d window', '64%', '18%', '$8.40', 'Unavailable', 'Resets in', 'Reset passed'):
        assert value in sheet
    js("document.getElementById('usage-close').click()")
    wait("document.activeElement.id==='usage-strip'")

    js("document.getElementById('usage-strip').click()")
    for event in ('mousePressed', 'mouseReleased'):
        client.call('Input.dispatchMouseEvent', type=event, x=30, y=45, button='left', clickCount=1)
    wait("!document.getElementById('usage-sheet').open&&document.getElementById('usage-strip').getAttribute('aria-expanded')==='false'")

    fixture['usage'] = None
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.getElementById('usage-strip').innerText.includes('Usage unavailable')")
    assert '64%' not in js("document.getElementById('usage-strip').innerText")


def test_usage_strip_wraps_longer_readings_at_phone_width(phone):
    _, js, wait, fixture = phone
    now = int(time.time())
    fixture['usage'] = dict(server_time=now, fresh_seconds=300, providers=[
        dict(provider=provider, status='unavailable', windows=[])
        for provider in ('claude', 'codex', 'antigravity', 'dsh')
    ])
    js('location.reload()')
    wait("document.querySelectorAll('.usage-compact').length===4")
    assert js("""(() => {
        const strip = document.querySelector('#usage-strip').getBoundingClientRect();
        const entries = Array.from(document.querySelectorAll('.usage-compact'), el => el.getBoundingClientRect());
        return innerWidth === 390 && document.body.scrollWidth === 390
          && entries.at(-1).top > entries[0].top
          && entries.every(entry => entry.right <= strip.right && entry.bottom <= strip.bottom);
    })()""")
