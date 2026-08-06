/* The demo, narrated.
 *
 * The fixture makes the page move; it does not tell anyone what they are
 * looking at. A visitor who lands mid-story sees six cards, some arcs and
 * a graph, and has no reason to know that the interesting thing just
 * happened in the corner they were not looking at.
 *
 * The story is scripted and looping, so this file knows what is happening
 * at every second. It says so, and rings the thing it is talking about.
 *
 * Rules it keeps:
 *   Nothing here is a product feature. It only ever adds an overlay and a
 *   ring; it never changes what the dashboard does or shows.
 *   A beat whose target is not on screen still shows its caption. Selectors
 *   go stale when the page is edited, and a stale selector must cost a ring,
 *   not the narration.
 *
 * Loads on every page, returns immediately unless ?demo=1 is present.
 */
(function () {
  'use strict';
  if (new URLSearchParams(location.search).get('demo') !== '1' &&
      !window.AGENTSTACK_DEMO_FORCE) return;

  var CARD = {
    en: '<h2>This is a demo — no machines behind it</h2>' +
        '<p>Nine agents, a four-minute story on a loop. The data is written, ' +
        'not recorded from anyone’s laptop, and every control is live but ' +
        'inert: pressing one changes nothing.</p><ul>' +
        '<li><b>Click a card</b> to read what that agent is doing, line by line.</li>' +
        '<li><b>⊹ Network</b> shows who spawned whom and what they said.</li>' +
        '<li><b>▸ NEW</b> opens the form you would really start an agent with.</li>' +
        '</ul>',
    ja: '<h2>これはデモです — 後ろに実機はありません</h2>' +
        '<p>エージェント9体、4分で一周する台本です。データは書き下ろしたもので、' +
        '誰かの端末から採ったものではありません。操作はすべて生きていますが、' +
        '押しても何も起きません。</p><ul>' +
        '<li><b>カードを押す</b>と、そのエージェントの作業を1行ずつ読めます。</li>' +
        '<li><b>⊹ Network</b> で、誰が誰を起動し何を言ったかが見えます。</li>' +
        '<li><b>▸ NEW</b> は実際にエージェントを起動するときのフォームです。</li>' +
        '</ul>',
  };
  var START_BTN = { en: 'START WATCHING', ja: '見てみる' };
  var BLOCKED = { en: 'nothing was started, stopped or changed',
                  ja: '何も起動・停止・変更されていません' };

  var CSS = [
    /* Clear of #toast, which the page parks at bottom:30px — the two
       overlapped, and the collision only showed when a button was pressed. */
    '#demo-strip{position:fixed;left:50%;bottom:88px;transform:translateX(-50%);',
    '  z-index:60;max-width:min(760px,92vw);display:flex;align-items:center;',
    '  gap:14px;padding:11px 16px;border-radius:10px;',
    '  background:rgba(10,12,16,.92);border:1px solid rgba(212,168,84,.34);',
    '  box-shadow:0 10px 34px rgba(0,0,0,.55);',
    '  font:13px/1.5 ui-sans-serif,system-ui,sans-serif;color:#e8e2d4;',
    '  backdrop-filter:blur(6px);opacity:0;transition:opacity .4s}',
    '#demo-strip.on{opacity:1}',
    '#demo-strip .tag{flex:none;font:10px/1 "IBM Plex Mono",monospace;',
    '  letter-spacing:.18em;color:#d4a854;border:1px solid rgba(212,168,84,.45);',
    '  border-radius:3px;padding:5px 7px}',
    '#demo-strip .txt{flex:1}',
    '#demo-strip .hint{flex:none;font:10.5px/1 "IBM Plex Mono",monospace;',
    '  letter-spacing:.1em;color:#9a9081}',
    '#demo-bar{position:absolute;left:0;bottom:0;height:2px;width:0;',
    '  background:rgba(212,168,84,.7);border-radius:0 2px 2px 0;',
    '  transition:width .9s linear}',
    '#demo-ring{position:fixed;z-index:59;pointer-events:none;border-radius:8px;',
    '  border:2px solid rgba(212,168,84,.7);',
    '  transition:left .35s,top .35s,width .35s,height .35s;',
    '  animation:demoPulse 2.4s ease-in-out infinite}',
    '@keyframes demoPulse{0%,100%{border-color:rgba(212,168,84,.22)}',
    '  50%{border-color:rgba(212,168,84,.8)}}',
    '#demo-card{position:fixed;inset:0;z-index:61;display:flex;',
    '  align-items:center;justify-content:center;background:rgba(6,7,9,.72);',
    '  backdrop-filter:blur(3px);font:14px/1.65 ui-sans-serif,system-ui,sans-serif}',
    '#demo-card .box{max-width:min(520px,88vw);padding:26px 28px;',
    '  border-radius:12px;background:#0d0f13;color:#e8e2d4;',
    '  border:1px solid rgba(212,168,84,.34);box-shadow:0 18px 50px rgba(0,0,0,.6)}',
    '#demo-card h2{margin:0 0 12px;font:600 16px/1.3 ui-sans-serif,system-ui;',
    '  letter-spacing:.02em;color:#d4a854}',
    '#demo-card p{margin:0 0 12px}',
    '#demo-card ul{margin:0 0 18px;padding-left:19px}',
    '#demo-card li{margin:5px 0}',
    '#demo-card button{appearance:none;border:1px solid rgba(212,168,84,.5);',
    '  background:rgba(212,168,84,.16);color:#e8e2d4;border-radius:6px;',
    '  padding:8px 18px;font:12px/1 "IBM Plex Mono",monospace;',
    '  letter-spacing:.12em;cursor:pointer}',
    '#demo-card button:hover{background:rgba(212,168,84,.28)}',
    '#demo-card .row{display:flex;align-items:center;justify-content:space-between;',
    '  gap:14px}',
    '#demo-strip .lang,#demo-card .pick{flex:none;display:flex;gap:4px}',
    '#demo-strip .lang button,#demo-card .pick button{appearance:none;',
    '  border:1px solid rgba(212,168,84,.28);background:none;color:#9a9081;',
    '  border-radius:4px;padding:4px 8px;cursor:pointer;',
    '  font:10.5px/1 "IBM Plex Mono",monospace;letter-spacing:.08em}',
    '#demo-strip .lang button.on,#demo-card .pick button.on{color:#0d0f13;',
    '  background:rgba(212,168,84,.85);border-color:rgba(212,168,84,.85)}',
  ].join('\n');

  function el(tag, attrs, html) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (html != null) n.innerHTML = html;
    return n;
  }

  /* Beats come from the active story, so switching story re-narrates. */
  function beats() {
    var d = window.AGENTSTACK_DEMO;
    return (d && d.beats && d.beats()) || [];
  }

  function beatAt(t) {
    var b = beats();
    if (!b.length) return null;
    var cur = b[b.length - 1];
    for (var i = 0; i < b.length; i++) if (b[i].at <= t) cur = b[i];
    return cur;
  }

  var strip, bar, txt, hint, toggle, ringEl;
  var shown = null, shownLang = null, lookSel = null;

  /* The ring is its own box laid over the target rather than a class on it.
     The deck reassigns its own className on every poll, so a class set here
     survives about a second — long enough to pass a check written right
     after setting it, and gone by the time anyone looks. */
  function ring(sel) {
    lookSel = sel || null;
    place();
  }

  function place() {
    var n = lookSel ? document.querySelector(lookSel) : null;
    if (!n) { ringEl.style.display = 'none'; return; }
    var r = n.getBoundingClientRect();
    if (!r.width || !r.height) { ringEl.style.display = 'none'; return; }
    ringEl.style.display = '';
    ringEl.style.left = (r.left - 4) + 'px';
    ringEl.style.top = (r.top - 4) + 'px';
    ringEl.style.width = (r.width + 8) + 'px';
    ringEl.style.height = (r.height + 8) + 'px';
  }

  function lang() {
    var d = window.AGENTSTACK_DEMO;
    return d && d.lang() === 'ja' ? 'ja' : 'en';
  }

  function tick() {
    var d = window.AGENTSTACK_DEMO;
    if (!d) return;
    var t = d.phase(), b = beatAt(t), l = lang();
    bar.style.width = (t / d.loop() * 100).toFixed(2) + '%';
    if (!b) return;
    if (b !== shown || l !== shownLang) {
      shown = b; shownLang = l; txt.textContent = b[l];
    }
    var here = document.body.dataset.view || 'deck';
    hint.textContent = b.view && b.view !== here ? '⊹ NETWORK →' : '';
    ring(b.look);
  }

  /* Switching language re-labels the toggle and the caption immediately; the
     deck and graph pick it up on their next poll, and a pane on its next
     open. Nothing is reloaded, so the story keeps its place. */
  function switchTo(l) {
    var d = window.AGENTSTACK_DEMO;
    if (!d) return;
    d.setLang(l);
    paintToggle();
    tick();
    var card = document.getElementById('demo-card');
    if (card) paintCard(card);
  }

  function paintToggle() {
    if (!toggle) return;
    var cur = lang();
    Array.prototype.forEach.call(toggle.querySelectorAll('button'),
      function (b) { b.classList.toggle('on', b.dataset.lang === cur); });
  }

  function paintCard(card) {
    var l = lang();
    card.querySelector('.copy').innerHTML = CARD[l];
    card.querySelector('.go').textContent = START_BTN[l];
    paintToggle();
  }

  function build() {
    document.head.appendChild(el('style', {}, CSS));

    strip = el('div', { id: 'demo-strip' },
      '<span class="tag">DEMO</span><span class="txt"></span>' +
      '<span class="hint"></span>' +
      '<span class="lang"><button type="button" data-lang="en">EN</button>' +
      '<button type="button" data-lang="ja">日本語</button></span>' +
      '<i id="demo-bar"></i>');
    document.body.appendChild(strip);
    ringEl = el('div', { id: 'demo-ring' });
    ringEl.style.display = 'none';
    document.body.appendChild(ringEl);

    bar = strip.querySelector('#demo-bar');
    txt = strip.querySelector('.txt');
    hint = strip.querySelector('.hint');
    toggle = strip.querySelector('.lang');
    toggle.addEventListener('click', function (e) {
      var b = e.target.closest('button[data-lang]');
      if (b) switchTo(b.dataset.lang);
    });

    var card = el('div', { id: 'demo-card' },
      '<div class="box"><div class="copy"></div>' +
      '<div class="row"><button type="button" class="go"></button>' +
      '<span class="pick"><button type="button" data-lang="en">EN</button>' +
      '<button type="button" data-lang="ja">日本語</button></span></div></div>');
    card.querySelector('.go').addEventListener('click', function () {
      card.remove();
      strip.classList.add('on');
      /* Rewind to the opening so the story starts when watching does. */
      if (window.AGENTSTACK_DEMO && window.AGENTSTACK_DEMO.restart) {
        window.AGENTSTACK_DEMO.restart();
        tick();
      }
    });
    card.querySelector('.pick').addEventListener('click', function (e) {
      var b = e.target.closest('button[data-lang]');
      if (b) switchTo(b.dataset.lang);
    });
    document.body.appendChild(card);
    paintCard(card);

    window.addEventListener('demo:blocked', function () {
      if (typeof toast === 'function') toast('◦ DEMO', BLOCKED[lang()]);
    });

    addEventListener('scroll', place, true);
    addEventListener('resize', place);

    tick();
    setInterval(tick, 900);
  }

  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', build);
  else build();

  window.AGENTSTACK_TOUR = { beats: beats, beatAt: beatAt,
                             card: CARD, switchTo: switchTo };
})();
