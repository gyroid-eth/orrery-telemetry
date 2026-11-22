(() => {
  'use strict';

  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const fields = new WeakMap();
  const minHeight = 44, maxHeight = 120;
  const spinnerWidth = 44, receivedWidth = 166;
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const unit = value => clamp(value, 0, 1);

  function spring(value, target, stiffness, damping, velocity = 0) {
    return {value, target, stiffness, damping, velocity};
  }

  function animate(element, springs, paint, firstFrame = 0) {
    let frame = 0, last = performance.now();
    const moving = track => Math.abs(track.value - track.target) > .001 || Math.abs(track.velocity) > .01;
    const settle = track => {track.value = track.target; track.velocity = 0;};

    function advance(seconds) {
      const dt = Math.min(seconds, 1 / 30) / 4;
      for (const track of springs) {
        if (reducedMotion.matches || !moving(track)) {
          settle(track);
          continue;
        }
        for (let step = 0; step < 4; step++) {
          const force = track.stiffness * (track.target - track.value) - track.damping * track.velocity;
          track.velocity += force * dt;
          track.value += track.velocity * dt;
        }
      }
      paint();
    }

    function tick(now) {
      if (!element.isConnected) return;
      advance(Math.max(0, now - last) / 1000);
      last = now;
      if (springs.some(moving)) frame = requestAnimationFrame(tick);
    }

    advance(firstFrame);
    if (springs.some(moving)) frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }

  function fit(field, animated) {
    const current = field.style.height, scrollTop = field.scrollTop;
    field.style.height = minHeight + 'px';
    const target = clamp(field.scrollHeight, minHeight, maxHeight);
    field.style.height = current;
    field.scrollTop = scrollTop;

    const previous = fields.get(field);
    if (animated && previous?.height.target === target) return;
    previous?.stop();
    if (!animated || reducedMotion.matches || !current) {
      fields.delete(field);
      field.style.height = target + 'px';
      return;
    }

    const height = spring(parseFloat(current), target, 420, 34, previous?.height.velocity);
    const stop = animate(field, [height], () => {
      field.style.height = clamp(height.value, minHeight, maxHeight).toFixed(2) + 'px';
    }, 1 / 60);
    fields.set(field, {height, stop});
  }

  const node = (tag, className) => {
    const element = document.createElement(tag);
    element.className = className;
    return element;
  };

  function createSendFeedback() {
    let motion = null, reviewWidth = spinnerWidth, reviewTop = null, stop = null;
    let offset = spring(0, 0, 300, 22);

    function reset() {
      stop?.();
      stop = motion = null;
      reviewWidth = spinnerWidth;
      reviewTop = null;
      offset = spring(0, 0, 300, 22);
    }

    function begin(button) {
      reset();
      const rect = button?.getBoundingClientRect();
      if (!rect?.width) return;
      reviewWidth = rect.width;
      reviewTop = rect.top;
    }

    function show(box, phase) {
      const sending = phase === 'sending', accepted = phase === 'accepted';
      if (!sending && !accepted) {
        reset();
        return;
      }

      stop?.();
      if (motion?.phase !== phase) {
        motion = {
          phase,
          width: spring(motion?.width.value ?? reviewWidth, sending ? spinnerWidth : receivedWidth, 300, 22, motion?.width.velocity),
          label: spring(sending ? 1 : 0, 0, 400, 30),
          loading: spring(motion?.loading.value ?? 0, sending ? 1 : 0, 300, 28),
          result: spring(0, accepted ? 1 : 0, 220, 20),
          tint: spring(0, accepted ? 1 : 0, 200, 30),
          check: spring(0, accepted && reducedMotion.matches ? 1 : 0, 120, 20),
          checkAfter: accepted ? performance.now() + 150 : Infinity,
        };
      }
      const {width, label, loading, result, tint, check} = motion;
      const headline = box.querySelector('strong'), description = box.querySelector('small');
      const copy = node('div', 'reader-send-copy');
      const pill = node('div', 'reader-send-pill');
      const spinner = node('span', 'reader-send-spinner');
      const exit = node('span', 'reader-send-exit');
      spinner.setAttribute('aria-hidden', 'true');
      exit.setAttribute('aria-hidden', 'true');
      pill.append(spinner);

      let done, tick;
      if (accepted) {
        done = node('div', 'reader-send-result');
        const ns = 'http://www.w3.org/2000/svg', icon = document.createElementNS(ns, 'svg');
        tick = document.createElementNS(ns, 'path');
        icon.setAttribute('viewBox', '0 0 24 24');
        icon.setAttribute('width', '17');
        icon.setAttribute('height', '17');
        icon.setAttribute('aria-hidden', 'true');
        tick.setAttribute('d', 'm5 12 4.5 4.5L19 7');
        tick.setAttribute('pathLength', '1');
        icon.append(tick);
        done.append(icon, headline);
        pill.append(done);
      } else {
        exit.textContent = 'Send once';
        pill.append(exit);
        copy.append(headline);
      }
      copy.append(description);
      box.classList.add('reader-receipt-motion');
      box.append(pill, copy);
      if (reviewTop !== null) {
        offset.value = reviewTop - pill.getBoundingClientRect().top;
        reviewTop = null;
      }

      stop = animate(pill, [width, offset, label, loading, result, tint, check], () => {
        pill.style.width = Math.max(spinnerWidth, width.value).toFixed(2) + 'px';
        pill.style.transform = `translateY(${offset.value.toFixed(2)}px)`;
        pill.style.setProperty('--reader-send-success', (100 * unit(tint.value)).toFixed(2) + '%');
        exit.style.opacity = unit(label.value);
        exit.style.transform = `translateY(${(1 - label.value) * 14}px)`;
        spinner.style.opacity = unit(loading.value);
        spinner.style.scale = .6 + .4 * clamp(loading.value, 0, 1.2);
        if (done) {
          if (loading.value < .001) spinner.style.animationName = 'none';
          if (reducedMotion.matches || performance.now() >= motion.checkAfter) check.target = 1;
          done.style.opacity = unit(result.value);
          done.style.transform = `translateY(${(1 - result.value) * 12}px)`;
          tick.style.strokeDashoffset = 1 - unit(check.value);
        }
      });
    }

    return {begin, show, reset};
  }

  window.OrreryReaderSendMotion = {fit, createSendFeedback};
})();
