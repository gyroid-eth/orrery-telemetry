(() => {
  'use strict';

  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const running = new Set();
  const roll = [260, 20, 580], icon = [420, 18, 580], width = [300, 24, 580];
  const word = [340, 26, 430], stagger = 70, flightMs = 610, confirmationMs = 1800;
  const clamp = (value, min = 0, max = 1) => Math.min(max, Math.max(min, value));
  const canAnimate = node => !!node?.animate && !reduce?.matches && !document.hidden;
  const svgNS = 'http://www.w3.org/2000/svg';
  const eastAsian = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}、。！？；，．…]/u;
  const wordRuns = /\s+|[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}、。！？；，．…]+|[^\s\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}、。！？；，．…]+/gu;
  const segmenter = typeof Intl.Segmenter === 'function' ? new Intl.Segmenter('ja', {granularity:'word'}) : null;
  const queued = [], flights = new WeakMap();
  let queueIndex = 0, frame = 0, nextWord = 0, frontier = null, following = false, scroll = null;

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
    finishReplies();
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

  function ranges(text) {
    if (!segmenter || !eastAsian.test(text))
      return Array.from(text.matchAll(/\S+/gu), match => [match.index, match.index+match[0].length]);
    return Array.from(text.matchAll(wordRuns)).flatMap(match => {
      if (!match[0].trim()) return [];
      if (!eastAsian.test(match[0])) return [[match.index, match.index+match[0].length]];
      return Array.from(segmenter.segment(match[0]), part => [match.index+part.index, match.index+part.index+part.segment.length]);
    });
  }
  function tokenize(body, className, limit = Infinity) {
    const demo = window.OrreryDemo?.on ? window.OrreryDemo : null;
    const cues = [], tails = [];
    if (demo && !demo.projectText) return {cues, tails};
    demo?.maskContent?.(body);
    const skip = '.conversation-code-head, [aria-hidden="true"], [hidden]' +
      (className==='reader-reply-word' ? ', button' : '');
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
      acceptNode: node => node.parentElement.closest(skip) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT
    });
    const runs = [];
    for (let node = walker.nextNode(); node && limit > 0; node = walker.nextNode()) {
      const projection = demo?.projectText(node), shown = projection?.text ?? node.data;
      const words = ranges(shown).slice(0, limit);
      if (words.length) runs.push({node, words, shown, projection});
      limit -= words.length;
    }
    for (const {node, words, shown, projection} of runs) {
      const fragment = document.createDocumentFragment();
      const part = (from, to) => {
        const text = document.createTextNode(shown.slice(from, to));
        projection?.write(text, from, to);
        return text;
      };
      let offset = 0;
      for (const [start,end] of words) {
        const cue = span(className);
        cue.append(part(start,end));
        fragment.append(part(offset,start),cue);
        cues.push(cue);
        offset = end;
      }
      const rest = part(offset, shown.length);
      if (className==='reader-mail-word' && rest.data.trim()){
        const tail=span('');tail.append(rest);fragment.append(tail);tails.push(tail);
      } else fragment.append(rest);
      node.replaceWith(fragment);
    }
    return {cues,tails};
  }
  function readingOrder(cues) {
    const ordered = [];
    for (let index=0;index<cues.length;) {
      const cue=cues[index++],row=cue.closest('tr');
      if (!row){ordered.push(cue);continue;}
      const line=[cue];
      while (index<cues.length&&cues[index].closest('tr')===row) line.push(cues[index++]);
      // A wrapped left cell does not precede the top line of the right cell.
      const positions=line.map(node=>({node,box:node.getBoundingClientRect()})).sort((a,b)=>a.box.top-b.box.top);
      for (let start=0;start<positions.length;){
        let end=start+1;
        while (end<positions.length&&positions[end].box.top-positions[start].box.top<5) end++;
        ordered.push(...positions.slice(start,end).sort((a,b)=>a.box.left-b.box.left).map(entry=>entry.node));
        start=end;
      }
    }
    return ordered;
  }
  const wordFrame = value => ({
    opacity:clamp(value), top:6*(1-value)+'px', filter:'blur('+4*(1-clamp(value))+'px)',
  });
  const rise = (node, px, duration, delay=0, spring=[300,22]) => animate(node,[...spring,duration],
    value=>({...translate(px*(1-value)),opacity:clamp(value)}),delay);
  function unqueue({cue,decorations}) {
    delete cue.dataset.replyQueued;
    for (const decoration of decorations) delete decoration.dataset.replyQueued;
  }
  function finishReplies() {
    cancelAnimationFrame(frame);frame=nextWord=0;
    for (;queueIndex<queued.length;queueIndex++) unqueue(queued[queueIndex]);
    queued.length=queueIndex=0;following=false;frontier=scroll=null;
  }
  function keepReplyInView() {
    if (!frontier?.isConnected || !scroll?.isConnected || !following) return false;
    const inset=parseFloat(getComputedStyle(scroll).scrollPaddingBottom)||0;
    const overflow=frontier.getBoundingClientRect().bottom-scroll.getBoundingClientRect().bottom+inset;
    if (overflow>0) scroll.scrollTop+=overflow;
    return true;
  }
  function revealNext() {
    frame=0;
    if (document.hidden||reduce?.matches){finishReplies();return;}
    while (queueIndex<queued.length&&!queued[queueIndex].cue.isConnected) unqueue(queued[queueIndex++]);
    if (performance.now()>=nextWord&&queueIndex<queued.length){
      const item=queued[queueIndex++];unqueue(item);
      animate(item.cue,word,wordFrame);
      for(const decoration of item.decorations)rise(decoration,0,190);
      nextWord=performance.now()+item.interval;
      frontier=item.cue;keepReplyInView();
    }
    if(queueIndex<queued.length)frame=requestAnimationFrame(revealNext);
    else{
      if(following&&scroll?.isConnected)scroll.scrollTop=scroll.scrollHeight;
      queued.length=queueIndex=0;following=false;frontier=scroll=null;
    }
  }
  function revealReply(body, list) {
    if (!canAnimate(body)) return;
    const cues=readingOrder(tokenize(body,'reader-reply-word').cues);
    if(!cues.length)return;
    const interval=Math.min(stagger,Math.max(34,820/Math.max(1,cues.length-1))),marked=new Set();
    for(const cue of cues){
      cue.dataset.replyQueued='';
      const decorations=[];
      for(let block=cue.closest('li,.conversation-table,.conversation-code');block;
        block=block.parentElement?.closest('li,.conversation-table,.conversation-code')){
        if(marked.has(block))continue;
        marked.add(block);block.dataset.replyQueued='';decorations.push(block);
      }
      queued.push({cue,decorations,interval});
    }
    if(!frontier?.isConnected)frontier=cues[0];
    scroll=list;following=true;
    if(!frame)frame=requestAnimationFrame(revealNext);
  }
  function revealMail(article) {
    if(!canAnimate(article))return;
    const demo=window.OrreryDemo?.on?window.OrreryDemo:null;
    demo?.maskContent?.(article);
    for(const row of article.querySelectorAll('.conversation-mail')){
      rise(row.querySelector('.conversation-mail-sender'),0,250);
      animate(row.querySelector('.conversation-mail-arrow'),[190,19,450],
        value=>({transform:'translateX('+(-8*(1-value))+'px)',opacity:clamp(value)}),85);
      for(const recipient of row.querySelectorAll('.conversation-mail-recipients :is(.conversation-mail-peer,.conversation-mail-more)'))
        rise(recipient,4,330,170);
      const subject=row.querySelector('.conversation-mail-subject');
      const {cues,tails}=tokenize(subject,'reader-mail-word',4);
      rise(subject,0,220,245);
      cues.forEach((cue,index)=>animate(cue,word,wordFrame,245+stagger*index));
      for(const tail of tails)rise(tail,0,220,245+stagger*cues.length);
      rise(row.querySelector('.conversation-mail-importance'),0,260,275,[420,32]);
    }
  }
  function flyPrompt(row, origin, duration=flightMs) {
    if(!origin?.width||!row?.isConnected||!canAnimate(row))return;
    if(window.OrreryDemo?.on)window.OrreryDemo.maskContent?.(row);
    const box=(row.querySelector('.conversation-reference-body')||row).getBoundingClientRect();
    const x=origin.right-box.right,y=origin.top-box.top,opacity=origin.opacity??.72;
    if(!box.width||Math.abs(x)+Math.abs(y)<1)return;
    const effect=animate(row,[300,22,duration],value=>({
      transform:'translate('+((1-value)*x)+'px,'+((1-value)*y)+'px)',opacity:opacity+(1-opacity)*clamp(value),
    }));
    if(effect)flights.set(row,effect);
  }
  function promptPosition(row) {
    const flight=flights.get(row);
    if(flight?.playState!=='running')return null;
    const remaining=flight.effect.getTiming().duration-(flight.currentTime||0);
    if(remaining<55)return null;
    const {top,right,width}=(row.querySelector('.conversation-reference-body')||row).getBoundingClientRect();
    return {top,right,width,remaining,opacity:Number(getComputedStyle(row).opacity)};
  }
  function acknowledge(heading) {
    if(!canAnimate(heading))return;
    const tick=span('reader-sent-ack','✓');tick.setAttribute('aria-hidden','true');
    tick.title='Received by terminal';heading.append(tick);
    const show=rise(tick,4,340);
    const remove=()=>tick.remove();
    show.finished.then(()=>{
      const fade=animate(tick,[300,22,240],value=>({opacity:1-clamp(value)}),600);
      if(fade)fade.finished.then(remove,remove);else remove();
    },remove);
  }

  window.OrreryReaderReplyMotion = Object.freeze({enhanceCopies,confirmReference,revealReply,revealMail,
    flyPrompt,promptPosition,acknowledge,keepReplyInView,finishReplies,followingReply:()=>following,
    stopFollowing:()=>{following=false;}});
})();
