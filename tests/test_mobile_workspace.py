"""The responsive layout and theme use the packaged mobile page."""
from mobile_reader_browser import phone  # noqa: F401


def test_phone_activity_and_tablet_split_use_the_same_views(phone):
    client, js, wait, state = phone
    js('OrreryMobileTerminal.close()')
    wait("!document.querySelector('#terminal-sheet').open")
    first_minute = 1_790_400_000
    state['snapshot']['timeline']['mail'] = [
        dict(key=f'{index:032x}', source='SampleAgent', target='Peer', kind='to', event_at=stamp)
        for index, stamp in enumerate((first_minute + 2, first_minute + 1, first_minute - 1), 1)
    ]
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelectorAll('.activity-mail').length === 3")
    assert js("""(() => {
        const graph = document.querySelector('#net').getBoundingClientRect();
        const sidebar = document.querySelector('.mobile-sidebar').getBoundingClientRect();
        return graph.bottom <= sidebar.top && sidebar.bottom <= innerHeight &&
            document.querySelector('#activity').hidden &&
            document.querySelector('#net').contains(document.querySelector('#summary'));
    })()""")

    js("document.querySelector('button[data-view=activity]').click()")
    assert js("!document.querySelector('#activity').hidden && document.querySelector('#graph-panel').hidden")
    assert js("document.activeElement.dataset.view") == 'graph'
    js("window.timeVisibility = () => Array.from(document.querySelectorAll('.activity-mail time'), "
       "stamp => getComputedStyle(stamp).visibility)")
    assert js('timeVisibility()') == ['visible', 'visible', 'visible']
    js("""window.recipientOnSameLine = () => {
        const people = document.querySelector('.activity-mail').querySelectorAll('.person');
        const [from, to] = Array.from(people, person => person.getBoundingClientRect());
        return Math.abs(from.top + from.height / 2 - to.top - to.height / 2) < 1;
    };""")
    assert js('recipientOnSameLine()')
    assert js("""(() => {
        const row = document.querySelector('.activity-mail');
        const sender = row.querySelector('.person .name').getBoundingClientRect();
        const arrow = row.querySelector('.mail-forward').getBoundingClientRect();
        const recipient = row.querySelector('.route-to .face').getBoundingClientRect();
        return Math.abs(arrow.left + arrow.width / 2 - (sender.right + recipient.left) / 2) < 3;
    })()""")

    for width, height in [(820, 1180), (1180, 820)]:
        client.call('Emulation.setDeviceMetricsOverride', width=width, height=height,
                    deviceScaleFactor=2, mobile=True)
        wait("""(() => {
            const graph = document.querySelector('#net').getBoundingClientRect();
            const sidebar = document.querySelector('.mobile-sidebar').getBoundingClientRect();
            return !document.querySelector('#activity').hidden &&
                document.querySelector('.sidebar-head > .activity-head') !== null &&
                graph.width > 480 && Math.abs(sidebar.top - graph.top) < 1 &&
                graph.right <= sidebar.left && sidebar.right <= innerWidth &&
                document.documentElement.scrollWidth <= innerWidth;
        })()""")
        assert js('timeVisibility()') == ['visible', 'hidden', 'visible']
        assert not js('recipientOnSameLine()')
        assert js("getComputedStyle(document.querySelector('.activity-head .period-trigger')).display") == 'none'
        assert js("""(() => {
            const stamp = document.querySelector('.activity-mail .repeat-minute');
            return stamp.closest('button').getAttribute('aria-label').endsWith(stamp.textContent);
        })()""")

    client.call('Emulation.setDeviceMetricsOverride', width=390, height=844,
                deviceScaleFactor=2, mobile=True)
    wait("document.querySelector('#graph-panel').hidden && !document.querySelector('#activity').hidden")
    assert js("document.querySelector('#activity > .activity-head') !== null")
    assert js('timeVisibility()') == ['visible', 'visible', 'visible']
    assert js('recipientOnSameLine()')
    assert js("getComputedStyle(document.querySelector('.activity-head .period-trigger')).display") != 'none'
    js("document.querySelector('button[data-view=graph]').click()")
    assert js("!document.querySelector('#graph-panel').hidden && document.querySelector('#activity').hidden")


def test_dark_default_and_explicit_light_persist_across_reload(phone):
    client, js, wait, _ = phone
    js('OrreryMobileTerminal.close()')
    client.call('Emulation.setEmulatedMedia', features=[dict(name='prefers-color-scheme', value='light')])
    client.call('Page.reload', ignoreCache=True)
    wait("document.querySelector('#mobile-theme')?.getAttribute('aria-label') === 'Use light theme'")
    assert js("matchMedia('(prefers-color-scheme: light)').matches")
    assert js("document.documentElement.dataset.colorTheme") == 'dark'
    assert js("getComputedStyle(document.documentElement).colorScheme") == 'dark'

    js("document.querySelector('#mobile-theme').click()")
    assert js("getComputedStyle(document.documentElement).colorScheme") == 'light'
    assert js("document.querySelector('meta[name=theme-color]').content") == '#f5efe1'
    assert js("document.querySelector('.mobile-brand img').getAttribute('src')") == '/assets/orbit-light.svg'
    origin = js('performance.timeOrigin')
    client.call('Page.reload', ignoreCache=True)
    wait(f"performance.timeOrigin !== {origin} && "
         "document.querySelector('#mobile-theme')?.getAttribute('aria-label') === 'Use dark theme'")
    assert js("document.documentElement.dataset.colorTheme") == 'light'

    js("document.querySelector('#mobile-theme').click()")
    assert js("getComputedStyle(document.documentElement).colorScheme") == 'dark'
    assert js("document.querySelector('.mobile-brand img').getAttribute('src')") == '/assets/orbit.svg'
