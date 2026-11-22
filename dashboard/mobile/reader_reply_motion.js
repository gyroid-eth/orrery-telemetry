(() => {
  'use strict';

  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const running = new Set();
  const roll = [260, 20, 580], icon = [420, 18, 580], width = [300, 24, 580];
  const word = [340, 26, 460], stagger = 70, maxSteps = 12, confirmationMs = 1800;
  const clamp = (value, min = 0, max = 1) => Math.min(max, Math.max(min, value));
  const canAnimate = node => !!node?.animate && !reduce?.matches && !document.hidden;
  const svgNS = 'http://www.w3.org/2000/svg';

  function span(className, text) {
    const node = document.createElement('span');
    node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function glyph(className, path) {
    const svg = document.createElementNS(svgNS, 'svg');
    const line = document.createElementNS(svgNS, 'path');
    svg.setAttribute('class', className);
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    line.setAttribute('d', path);
    svg.append(line);
    return svg;
  }

  function check() {
    return glyph('reader-confirmation-check', 'm5 12 4.5 4.5L19 7');
  }

  function status() {
    const region = span('sr-only');
    region.setAttribute('role', 'status');
    return region;
  }

  // Sample the unit-mass spring from rest and let the browser run it once.
  function animate(node, [stiffness, damping, duration], frame, delay = 0) {
    if (!canAnimate(node)) return;
    const decay = damping / 2, frequency = Math.sqrt(stiffness - decay ** 2);
    const samples = Math.ceil(duration / 16);
    const frames = Array.from({length: samples + 1}, (_, i) => {
      const time = i * duration / (1000 * samples);
      const value = i === samples ? 1 :
        1 - Math.exp(-decay * time) * (Math.cos(frequency * time) + decay / frequency * Math.sin(frequency * time));
      return frame(value);
    });
    const effect = node.animate(frames, {duration, delay, fill: 'backwards'});
    running.add(effect);
    const done = () => running.delete(effect);
    effect.addEventListener('finish', done, {once: true});
    effect.addEventListener('cancel', done, {once: true});
    return effect;
  }

  function cancelMotion() {
    for (const effect of running) effect.cancel();
    running.clear();
  }

  reduce?.addEventListener?.('change', () => { if (reduce.matches) cancelMotion(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) cancelMotion(); });

  const translate = value => ({transform: 'translateY(' + value + 'px)'});
  const checkFrame = value => ({
    opacity: clamp(value * 1.6 - .4),
    transform: 'scale(' + (.4 + .6 * clamp(value, -.2, 1.2)) + ')'
  });

  function enhanceCopies(host) {
    for (const button of host.querySelectorAll('.conversation-code-copy')) {
      if (button.parentElement.classList.contains('reader-code-copy')) continue;
      const wrapper = span('reader-code-copy'), face = span('reader-copy-face');
      face.setAttribute('aria-hidden', 'true');
      face.hidden = true;
      const icons = span('reader-copy-icons');
      const copy = glyph('reader-copy-symbol', 'M8 8H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2v-2M10 4h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z');
      const copied = check();
      icons.append(copy, copied);
      const label = span('reader-copy-roll'), words = span('reader-copy-words');
      words.append(span('', 'Copy'), span('reader-copy-confirmed', 'Copied'));
      label.append(words);
      face.append(icons, label, span('reader-copy-error', 'Copy unavailable'));
      const announcement = status();
      button.before(wrapper);
      wrapper.append(button, face, announcement);

      // The bundled renderer owns clipboard writes, including errors and the reset.
      let animations = [];
      const observer = new MutationObserver(() => {
        const result = button.textContent;
        const next = result === 'Copied' ? 'copied' : result === 'Copy unavailable' ? 'failed' : '';
        const previous = wrapper.dataset.result;
        if (next === previous) return;
        wrapper.dataset.result = next;
        announcement.textContent = next ? result : '';
        for (const effect of animations) effect?.cancel();
        animations = [];
        if (next === 'failed' || (previous === 'failed' && !next)) return;
        const progress = value => next === 'copied' ? value : 1 - value;
        animations = [
          animate(words, roll, value => translate(-16 * progress(value))),
          animate(label, width, value => ({width: (4 + 2 * progress(value)) + 'ch'})),
          animate(copy, icon, value => {
            const position = progress(value), visible = clamp(position);
            return {
              opacity: clamp(1 - position * 1.6),
              transform: 'scale(' + (1 - .5 * visible) + ') rotate(' + (-40 * visible) + 'deg)'
            };
          }),
          animate(copied, icon, value => checkFrame(progress(value)))
        ];
      });
      wrapper.dataset.result = '';
      observer.observe(button, {childList: true});
    }
  }

  function confirmReference(card) {
    const label = card?.querySelector('.reader-reference-label');
    if (!label || label.classList.contains('reader-reference-feedback')) return;

    const details = span('reader-reference-details');
    details.append(...label.childNodes);
    const cue = span('reader-reference-cue');
    const tick = check();
    cue.setAttribute('aria-hidden', 'true');
    cue.hidden = true;
    cue.append(tick, 'Reference added');
    const announcement = status();
    label.classList.add('reader-reference-feedback');
    label.append(details, cue, announcement);
    label.dataset.confirmed = 'true';

    const play = confirmed => {
      const progress = value => confirmed ? value : 1 - value;
      animate(details, roll, value => ({
        ...translate(-16 * progress(value)), opacity: 1 - clamp(progress(value))
      }));
      animate(cue, roll, value => ({
        ...translate(16 * (1 - progress(value))), opacity: clamp(progress(value))
      }));
      animate(tick, icon, value => checkFrame(progress(value)));
    };
    play(true);
    setTimeout(() => { if (label.isConnected) announcement.textContent = 'Reference added'; }, 0);
    setTimeout(() => {
      label.dataset.confirmed = 'false';
      if (label.isConnected) play(false);
    }, confirmationMs);
  }

  function revealReply(body) {
    if (!canAnimate(body)) return;
    const skip = 'pre, code, button, a, table, .conversation-code, .conversation-table, [aria-hidden="true"]';
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
      acceptNode: node => node.parentElement.closest(skip) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT
    });
    const runs = [];
    let wordCount = 0, chars = 0;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      chars += node.length;
      if (chars > 4000 || runs.length >= 32) return;
      const words = Array.from(node.data.matchAll(/\S+/gu));
      if (!words.length) continue;
      runs.push({node, words});
      wordCount += words.length;
    }

    // Keep the stagger short by revealing groups in longer replies.
    const perStep = Math.ceil(wordCount / maxSteps);
    let index = 0;
    for (const {node, words} of runs) {
      const fragment = document.createDocumentFragment();
      const cues = [];
      let offset = 0;
      for (let i = 0; i < words.length;) {
        const count = Math.min(words.length - i, perStep - index % perStep);
        const first = words[i], last = words[i + count - 1], end = last.index + last[0].length;
        const cue = span('reader-reply-word', node.data.slice(first.index, end));
        fragment.append(node.data.slice(offset, first.index), cue);
        cues.push({cue, delay: Math.floor(index / perStep) * stagger});
        offset = end;
        i += count;
        index += count;
      }
      fragment.append(node.data.slice(offset));
      node.replaceWith(fragment);
      for (const {cue, delay} of cues) {
        animate(cue, word, value => ({
          opacity: clamp(value),
          top: 6 * (1 - value) + 'px',
          filter: 'blur(' + 4 * (1 - clamp(value)) + 'px)'
        }), delay);
      }
    }
  }

  window.OrreryReaderReplyMotion = Object.freeze({enhanceCopies, confirmReference, revealReply});
})();
