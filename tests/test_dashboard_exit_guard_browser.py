"""Deck EXIT pause in a browser: a redraw that is not a poll's (the filter)
can also move a card into a confirmed EXIT's place (review of #207)."""
import time

from test_dashboard_tour_cue import embedded  # noqa: F401  (the demo page, embedded, over CDP)


def test_a_filter_redraw_that_takes_the_card_away_starts_the_pause(embedded):  # noqa: F811
    _client, _evaluate, inner, wait = embedded
    names = inner("[...document.querySelectorAll('.bay')].map(c=>c.dataset.name)")
    assert len(names) >= 2, names
    first, other = names[0], names[1]
    # /api/exit holds its answer for the whole test.
    inner("""(()=>{window.__orig=window.fetch;
      window.fetch=(u,i)=>String(u).includes('/api/exit')?new Promise(()=>{}):window.__orig(u,i);})()""")
    press = "void exitAgent({stopPropagation(){}},%r)"
    inner(press % first)
    inner(press % first)
    # The confirming press's own pause runs out, the reply still pending.
    time.sleep(2.3)
    before = inner("document.body.classList.contains('exit-settling')")
    # Filtering by the other agent's name takes the confirmed card away.
    inner("""(()=>{const q=document.getElementById('q');q.value=%r;
      q.dispatchEvent(new Event('input',{bubbles:true}));})()""" % other)
    wait("!document.querySelector('.bay[data-name=\"%s\"]')" % first, 5)
    after = inner("document.body.classList.contains('exit-settling')")
    inner(press % other)
    other_armed = inner("exitingSet.has(%r)" % other)
    inner("(()=>{const q=document.getElementById('q');q.value='';q.dispatchEvent(new Event('input',{bubbles:true}));})()")
    assert before is False and after is True and other_armed is False, (before, after, other_armed)
