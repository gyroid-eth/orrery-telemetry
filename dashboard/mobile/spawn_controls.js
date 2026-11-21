// Plain DOM motion enhancements. The host keeps ownership of control state.
(() => {
'use strict';
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const unit = value => clamp(value, 0, 1);
const activeFrames = new Set();
let frameId = 0;
let previousFrame;

function runFrames(now) {
  frameId = 0;
  const delta = previousFrame === undefined ? 1 / 60 : Math.min((now - previousFrame) / 1000, 1 / 30);
  previousFrame = now;
  for (const task of activeFrames) {
    if (!task(delta)) activeFrames.delete(task);
  }
  if (activeFrames.size && !frameId) frameId = requestAnimationFrame(runFrames);
  if (!activeFrames.size) previousFrame = undefined;
}

function schedule(task) {
  activeFrames.add(task);
  if (!frameId) frameId = requestAnimationFrame(runFrames);
}

reducedMotion.addEventListener('change', () => {
  if (!reducedMotion.matches) return;
  for (const task of activeFrames) {
    task(0);
    activeFrames.delete(task);
  }
});

function spring(value, stiffness, damping) {
  return { value, goal: value, speed: 0, stiffness, damping };
}

function rest(motion, value = motion.goal) {
  motion.value = motion.goal = value;
  motion.speed = 0;
}

function advance(motion, delta) {
  if (reducedMotion.matches) {
    rest(motion);
    return false;
  }
  const slice = delta / 4;
  for (let i = 0; i < 4; i++) {
    const force = motion.stiffness * (motion.goal - motion.value) - motion.damping * motion.speed;
    motion.speed += force * slice;
    motion.value += motion.speed * slice;
  }
  if (Math.abs(motion.goal - motion.value) > .001 || Math.abs(motion.speed) > .015) return true;
  rest(motion);
  return false;
}

// Sample a group of springs during one shared frame pass, only while moving.
function springDriver(springs, render) {
  function frame(delta) {
    let moving = false;
    for (const motion of springs) moving = advance(motion, delta) || moving;
    render(moving);
    return moving;
  }
  return {
    play() {
      if (reducedMotion.matches) frame(0);
      else schedule(frame);
    },
    stop() {
      activeFrames.delete(frame);
    },
  };
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// The original range stays focusable, form-associated, and receives all input.
function mountScrubber(input, {output, formatValue, stops, stopLabels} = {}) {
  const root = input.parentElement;
  const art = element('span', 'om-range-art');
  art.setAttribute('aria-hidden', 'true');
  const track = element('span', 'om-range-track');
  const handle = element('span', 'om-range-handle');
  const thumb = element('span', 'om-range-thumb');
  const originalValueText = input.getAttribute('aria-valuetext');
  const listeners = new AbortController();
  const {signal} = listeners;
  let pointer;
  let resetFrame, tickKey;
  handle.append(thumb);
  track.append(element('span', 'om-range-fill'));
  const ticks = element('span', 'om-range-ticks');
  track.append(ticks);
  track.append(handle);
  art.append(track);
  root.classList.add('om-range');
  input.after(art);
  const hint = stopLabels && element('span', 'om-range-stop-label');
  if (hint) {
    hint.hidden = true;
    hint.setAttribute('aria-hidden', 'true');
    root.append(hint);
    input.addEventListener('pointermove', event => {
      const index = [...ticks.children].findIndex(tick => Math.abs(event.clientX - (tick.getBoundingClientRect().left + tick.offsetWidth/2)) < 8);
      const show = index >= 0 && pointer === undefined && event.pointerType === 'mouse';
      hint.hidden = !show;
      if (!show) return;
      hint.textContent = stopLabels[index];
      const x = event.clientX - root.getBoundingClientRect().left;
      hint.style.left = `clamp(38px,${x}px,calc(100% - 38px))`;
    }, {signal});
    input.addEventListener('pointerleave', () => {hint.hidden = true;}, {signal});
  }

  function sync() {
    const min = input.min && Number.isFinite(Number(input.min)) ? Number(input.min) : 0;
    const max = input.max && Number.isFinite(Number(input.max)) ? Number(input.max) : 100;
    const step = Number(input.step) || 1;
    const range = Math.max(0, max - min);
    const count = Math.min(7, Math.floor(range / step) + 1);
    const key = [min, max, step].join('/');
    if (tickKey !== key) {
      tickKey = key;
      const values = stops ?? Array.from({length:count}, (_,index) => min + index * range / Math.max(1,count - 1));
      ticks.replaceChildren();
      for (const value of values) {
        const tick = element('span', 'om-range-tick');
        tick.style.insetInlineStart = `${(range ? unit((value-min)/range) : 0)*100}%`;
        ticks.append(tick);
      }
    }
    const ratio = max > min ? unit((input.valueAsNumber - min) / (max - min)) : 0;
    root.style.setProperty('--om-range-fraction', ratio || 0);
    const value = formatValue?.(input.valueAsNumber) ?? input.value;
    if (formatValue) input.setAttribute('aria-valuetext', value);
    if (output) output.textContent = value;
  }

  input.addEventListener('input', sync, {signal});
  input.addEventListener('change', sync, {signal});
  input.addEventListener('pointerdown', event => {
    if (input.disabled || event.button !== 0 || pointer !== undefined) return;
    pointer = event.pointerId;
    if (hint) hint.hidden = true;
    root.classList.add('om-range-dragging');
  }, {signal});
  function release(event) {
    if (pointer !== event.pointerId) return;
    pointer = undefined;
    root.classList.remove('om-range-dragging');
  }
  window.addEventListener('pointerup', release, {capture: true, signal});
  window.addEventListener('pointercancel', release, {capture: true, signal});
  input.addEventListener('lostpointercapture', release, {signal});
  input.form?.addEventListener('reset', () => {
    cancelAnimationFrame(resetFrame);
    resetFrame = requestAnimationFrame(sync);
  }, {signal});
  const changes = new MutationObserver(sync);
  changes.observe(input, {attributes: true, attributeFilter: ['min', 'max', 'step', 'value']});
  sync();

  return {
    sync,
    destroy() {
      listeners.abort();
      changes.disconnect();
      cancelAnimationFrame(resetFrame);
      art.remove();
      hint?.remove();
      root.classList.remove('om-range', 'om-range-dragging');
      root.style.removeProperty('--om-range-fraction');
      if (originalValueText === null) input.removeAttribute('aria-valuetext');
      else input.setAttribute('aria-valuetext', originalValueText);
    },
  };
}

// Decorate an existing switch. The host sets aria-checked using its own handlers.
function mountToggle(button) {
  const track = element('span', 'om-toggle-track');
  const fill = element('span', 'om-toggle-fill');
  const knob = element('span', 'om-toggle-knob');
  track.setAttribute('aria-hidden', 'true');
  track.append(fill, knob);
  button.append(track);
  button.classList.add('om-toggle');
  const read = () => Number(button.getAttribute('aria-checked') === 'true');
  const position = spring(read(), 320, 18);
  const stretch = spring(0, 300, 20);

  function render() {
    const growth = unit(stretch.value) * 6;
    const x = position.value * 14 - (position.goal ? growth * unit(position.value) : 0);
    knob.style.transform = `translateX(${x}px) scaleX(${1 + growth / 16})`;
    fill.style.opacity = unit(position.value);
  }
  const driver = springDriver([position, stretch], render);

  function sync(animate = true) {
    const checked = read();
    if (!animate) {
      rest(position, checked);
      rest(stretch, 0);
      driver.stop();
      render();
    } else if (checked !== position.goal) {
      position.goal = checked;
      stretch.speed = 60;
      driver.play();
    }
  }

  const changes = new MutationObserver(() => sync());
  changes.observe(button, {attributes: true, attributeFilter: ['aria-checked']});
  render();

  return {
    sync,
    destroy() {
      changes.disconnect();
      driver.stop();
      track.remove();
      button.classList.remove('om-toggle');
    },
  };
}

let dropdownCount = 0;
const svgNS = 'http://www.w3.org/2000/svg';
function lineIcon(className, data) {
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('class', className);
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(svgNS, 'path');
  path.setAttribute('d', data);
  svg.append(path);
  return {svg, path};
}

// Progressive enhancement for single selects, including optgroups. Forms still use select.value.
function mountDropdown(select, {inline = false, selectedLabel = option => option?.label} = {}) {
  const noop = () => {};
  if (select.multiple || select.size > 1) return {refresh: noop, destroy: noop};

  const id = `om-choice-${++dropdownCount}`;
  const host = element('div', 'om-select');
  host.classList.toggle('om-select-inline', inline);
  const trigger = element('button', 'om-select-trigger');
  trigger.type = 'button';
  trigger.setAttribute('role', 'combobox');
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.setAttribute('aria-controls', `${id}-list`);
  const labels = [...select.labels];
  const labelledBy = select.getAttribute('aria-labelledby');
  const ariaLabel = select.getAttribute('aria-label');
  const generatedIds = new Map();
  if (!labelledBy && !ariaLabel) {
    for (const [index, label] of labels.entries()) {
      if (label.id) continue;
      label.id = `${id}-label-${index}`;
      generatedIds.set(label, label.id);
    }
  }
  const labelIds = labelledBy || (!ariaLabel ? labels.map(label => label.id).join(' ') : '');
  const accessibleLabel = ariaLabel || 'Choose an option';

  const display = element('span', 'om-select-value');
  const slots = [element('span', 'om-select-slot'), element('span', 'om-select-slot')];
  display.append(...slots);
  const chevron = lineIcon('om-select-chevron', 'm6 9 6 6 6-6').svg;
  trigger.append(display, chevron);
  const popup = element('div', 'om-select-popup');
  popup.hidden = true;
  popup.inert = true;
  const menu = element('div', 'om-select-menu');
  menu.id = `${id}-list`;
  menu.tabIndex = -1;
  menu.setAttribute('role', 'listbox');
  const highlight = element('span', 'om-select-highlight');
  highlight.setAttribute('aria-hidden', 'true');
  const error = element('span', 'om-select-error');
  error.id = `${id}-error`;
  error.setAttribute('role', 'alert');
  const originalDescription = select.getAttribute('aria-describedby') ?? '';
  if (originalDescription) trigger.setAttribute('aria-describedby', originalDescription);
  for (const node of [trigger, menu]) {
    if (labelIds) node.setAttribute('aria-labelledby', labelIds);
    else node.setAttribute('aria-label', accessibleLabel);
  }
  menu.append(highlight);
  popup.append(menu);
  host.append(trigger, popup, error);
  select.after(host);
  const previousTabIndex = select.getAttribute('tabindex');
  const previousAriaHidden = select.getAttribute('aria-hidden');
  trigger.tabIndex = select.tabIndex;
  select.tabIndex = -1;
  select.setAttribute('aria-hidden', 'true');
  select.classList.add('om-select-native');

  const height = spring(0, 300, 23);
  const alpha = spring(0, 400, 34);
  const angle = spring(0, 300, 18);
  const press = spring(1, 500, 18);
  const roll = spring(1, 340, 26);
  const top = spring(0, 620, 36);
  const bottom = spring(0, 620, 36);
  const highlightAlpha = spring(0, 400, 32);
  const springs = [height, alpha, angle, press, roll, top, bottom, highlightAlpha];
  const listeners = new AbortController();
  const {signal} = listeners;
  let rows = [];
  let open = false;
  let active = -1;
  let selectedOption;
  let selectedText;
  let front = 0;
  let menuHeight = 1;
  let rowTimers = [];
  let closeTimer;
  let resetFrame, sheetMotion;
  let prefix = '';
  let lastTyped = 0;

  function render(moving) {
    popup.style.height = `${Math.max(0, height.value)}px`;
    popup.style.opacity = unit(alpha.value);
    popup.style.transform = `scaleX(${.9 + .1 * clamp(height.value / menuHeight, 0, 1.06)})`;
    trigger.style.transform = `scale(${press.value})`;
    chevron.style.transform = `rotate(${180 * angle.value}deg)`;
    slots[front].style.opacity = unit(roll.value);
    slots[front].style.transform = `translateY(${(1 - roll.value) * 20}px)`;
    slots[front ^ 1].style.opacity = 1 - unit(roll.value);
    slots[front ^ 1].style.transform = `translateY(${-roll.value * 20}px)`;
    const firstEdge = Math.min(top.value, bottom.value - 16);
    highlight.style.height = `${Math.max(16, bottom.value - firstEdge)}px`;
    highlight.style.transform = `translateY(${firstEdge}px)`;
    highlight.style.opacity = unit(highlightAlpha.value) * unit(alpha.value);
    for (const {node, check, entry, tick} of rows) {
      node.style.opacity = unit(entry.value);
      node.style.transform = `translateY(${(entry.value - 1) * 14}px)`;
      check.style.strokeDashoffset = 22 * (1 - unit(tick.value));
    }
    if (!moving && !open && height.goal === 0) popup.hidden = true;
  }

  const driver = springDriver(springs, render);

  function layoutMenu() {
    const viewport = window.visualViewport;
    const visibleHeight = viewport?.height ?? window.innerHeight;
    if (inline) {
      menu.style.height = '';
      const preferred = Math.min(336, menu.offsetHeight + 2);
      menuHeight = Math.min(preferred, Math.max(92, Math.floor(visibleHeight * .42)));
      menu.style.height = `${menuHeight - 2}px`;
      return;
    }
    const bounds = trigger.getBoundingClientRect();
    const topEdge = viewport?.offsetTop ?? 0;
    const bottomEdge = topEdge + visibleHeight;
    const below = bottomEdge - bounds.bottom - 12;
    const above = bounds.top - topEdge - 12;
    const width = Math.min(Math.max(bounds.width, 250), window.innerWidth - 24);
    popup.style.width = `${width}px`;
    popup.style.left = `${Math.min(bounds.left, window.innerWidth - width - 12)}px`;
    menu.style.height = '';
    const preferred = Math.min(336, menu.offsetHeight + 2);
    const useAbove = below < preferred && above > below;
    menuHeight = Math.max(52, Math.min(preferred, useAbove ? above : below));
    popup.classList.toggle('om-select-above', useAbove);
    const edge = useAbove ? window.innerHeight - bounds.top : bounds.bottom;
    popup.style.setProperty('--om-select-edge', `${edge + 6}px`);
    menu.style.height = `${menuHeight - 2}px`;
  }

  function setActive(index, snap = false) {
    if (!rows[index] || rows[index].disabled || (active === index && !snap)) return;
    const node = rows[index].node;
    const start = node.offsetTop;
    const end = start + node.offsetHeight;
    if (snap || active < 0) {
      rest(top, start);
      rest(bottom, end);
    } else {
      const forward = index > active;
      Object.assign(forward ? bottom : top, {stiffness: 620, damping: 36});
      Object.assign(forward ? top : bottom, {stiffness: 200, damping: 20});
      top.goal = start;
      bottom.goal = end;
    }
    active = index;
    highlightAlpha.goal = 1;
    trigger.setAttribute('aria-activedescendant', node.id);
    if (start < menu.scrollTop + 6) menu.scrollTop = start - 6;
    else if (end > menu.scrollTop + menu.clientHeight - 6) menu.scrollTop = end - menu.clientHeight + 6;
    driver.play();
  }

  function animateRows(show) {
    for (const timer of rowTimers) clearTimeout(timer);
    rowTimers = [];
    for (const [index, row] of rows.entries()) {
      const reveal = () => {
        row.entry.goal = Number(show);
        driver.play();
      };
      if (reducedMotion.matches) reveal();
      else {
        const duration = show ? 50 + index * 45 : (rows.length - 1 - index) * 20;
        // Stagger visible rows without making long option lists wait.
        rowTimers.push(setTimeout(reveal, Math.min(duration, 275)));
      }
    }
  }

  function morph(opening) {
    sheetMotion?.cancel();
    if (inline || reducedMotion.matches || !popup.animate) return;
    const anchor = trigger.getBoundingClientRect();
    const left = parseFloat(popup.style.left);
    const right = left + popup.offsetWidth;
    const upper = popup.classList.contains('om-select-above');
    const top = upper ? anchor.top - menuHeight - 6 : anchor.bottom + 6;
    const bottom = top + menuHeight;
    const from = `inset(${anchor.top - top}px ${right - anchor.right}px ${bottom - anchor.bottom}px ${anchor.left - left}px round 999px)`;
    const full = 'inset(0px 0px 0px 0px round 10px)';
    const easing = getComputedStyle(host).getPropertyValue('--spawn-spring').trim();
    sheetMotion = popup.animate([{clipPath: from}, {clipPath: full}], {
      duration: opening ? 520 : 300,
      easing: opening ? easing : 'cubic-bezier(.4,0,.2,1)',
      direction: opening ? 'normal' : 'reverse',
    });
  }

  function show() {
    if (open || trigger.disabled) return;
    clearTimeout(closeTimer);
    open = true;
    popup.hidden = false;
    popup.inert = false;
    trigger.setAttribute('aria-expanded', 'true');
    layoutMenu();
    height.goal = menuHeight;
    alpha.goal = angle.goal = 1;
    if (!reducedMotion.matches) press.value = .965;
    const current = rows.findIndex(({option, disabled}) => option.selected && !disabled);
    setActive(current < 0 ? rows.findIndex(row => !row.disabled) : current, true);
    animateRows(true);
    morph(true);
    driver.play();
  }

  function hide({chosen = false} = {}) {
    if (!open) return;
    open = false;
    popup.inert = true;
    active = -1;
    prefix = '';
    trigger.setAttribute('aria-expanded', 'false');
    trigger.removeAttribute('aria-activedescendant');
    const fold = () => {
      height.goal = alpha.goal = angle.goal = highlightAlpha.goal = 0;
      animateRows(false);
      morph(false);
      driver.play();
    };
    if (chosen && !reducedMotion.matches) closeTimer = setTimeout(fold, 240);
    else fold();
  }

  function clearError() {
    if (!select.validity.valid) return;
    trigger.removeAttribute('aria-invalid');
    if (originalDescription) trigger.setAttribute('aria-describedby', originalDescription);
    else trigger.removeAttribute('aria-describedby');
    error.textContent = '';
  }

  function syncValue(animate = true) {
    const option = select.selectedOptions[0];
    const name = selectedLabel(option) || 'Choose an option';
    if (!animate) rest(roll, 1);
    if (option !== selectedOption || name !== selectedText) {
      if (animate && selectedText !== undefined && !reducedMotion.matches) {
        front ^= 1;
        roll.value = roll.speed = 0;
        roll.goal = 1;
      } else rest(roll, 1);
      selectedOption = option;
      selectedText = name;
      slots[front].textContent = name;
      slots[front].removeAttribute('aria-hidden');
      slots[front ^ 1].setAttribute('aria-hidden', 'true');
    }
    for (const row of rows) {
      const checked = Number(row.option === option);
      row.node.setAttribute('aria-selected', Boolean(checked));
      if (animate) row.tick.goal = checked;
      else rest(row.tick, checked);
    }
    trigger.setAttribute('aria-required', String(select.required));
    trigger.disabled = select.matches(':disabled') || !rows.some(row => !row.disabled);
    if (trigger.disabled) hide();
    clearError();
    render(false);
    if (animate) driver.play();
  }

  function refresh(animate = true) {
    const selectionChanged = selectedOption !== select.selectedOptions[0];
    const options = [...select.options].flatMap(option => {
      const group = option.parentElement.tagName === 'OPTGROUP' ? option.parentElement : null;
      if (option.hidden || group?.hidden) return [];
      return [{option, group, groupLabel: group?.label, label: option.label, disabled: option.matches(':disabled')}];
    });
    const changed = options.length !== rows.length || options.some((entry, index) => {
      const row = rows[index];
      return entry.option !== row.option || entry.group !== row.group || entry.groupLabel !== row.groupLabel ||
        entry.label !== row.label || entry.disabled !== row.disabled;
    });
    if (changed) {
      const wasActive = rows[active]?.option;
      const wasOpen = open;
      const groups = new Map();
      for (const timer of rowTimers) clearTimeout(timer);
      rowTimers = [];
      menu.replaceChildren(highlight);
      springs.length = 8;
      rows = options.map(({option, label, disabled, group, groupLabel}, index) => {
        let container = menu;
        if (group) {
          if (!groups.has(group)) {
            const box = element('div', 'om-select-group');
            const title = element('div', 'om-select-group-label', groupLabel);
            title.id = `${id}-group-${index}`;
            title.setAttribute('aria-hidden', 'true');
            box.setAttribute('role', 'group');
            box.setAttribute('aria-labelledby', title.id);
            box.append(title);
            menu.append(box);
            groups.set(group, box);
          }
          container = groups.get(group);
        }
        const node = element('div', 'om-select-option');
        const {svg, path} = lineIcon('om-select-check', 'm5 12.5 4.5 4.5L19 7.5');
        path.setAttribute('pathLength', '22');
        node.id = `${id}-option-${index}`;
        node.dataset.index = index;
        node.setAttribute('role', 'option');
        if (disabled) node.setAttribute('aria-disabled', 'true');
        node.append(element('span', 'om-select-option-label', label), svg);
        container.append(node);
        const entry = spring(Number(wasOpen), 300, 24);
        const tick = spring(Number(option.selected), 260, 26);
        springs.push(entry, tick);
        return {node, option, label, disabled, group, groupLabel, check: path, entry, tick};
      });
      if (wasOpen) {
        layoutMenu();
        height.goal = menuHeight;
        active = -1;
        highlightAlpha.goal = 0;
        trigger.removeAttribute('aria-activedescendant');
        const preferred = selectionChanged ? select.selectedOptions[0] : wasActive ?? selectedOption;
        const current = rows.findIndex(row => row.option === preferred && !row.disabled);
        setActive(current < 0 ? rows.findIndex(row => !row.disabled) : current, true);
      }
    }
    syncValue(animate);
    if (open && selectionChanged && !changed) {
      const current = rows.findIndex(row => row.option === selectedOption && !row.disabled);
      if (current >= 0) setActive(current, !animate);
    }
  }

  function choose(index) {
    const row = rows[index];
    if (!row || row.disabled) return;
    const changed = select.selectedIndex !== row.option.index;
    select.selectedIndex = row.option.index;
    if (!reducedMotion.matches) press.value = .955;
    syncValue();
    hide({chosen: true});
    if (changed) {
      select.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
      select.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }

  function move(direction, from = active) {
    for (let index = from + direction; index >= 0 && index < rows.length; index += direction) {
      if (!rows[index].disabled) {
        setActive(index);
        return;
      }
    }
  }

  function typeAhead(key) {
    const now = performance.now();
    prefix = now - lastTyped > 650 ? key : prefix + key;
    lastTyped = now;
    const repeated = [...prefix].every(letter => letter === key);
    const query = repeated ? key : prefix;
    const start = active < 0 ? 0 : active + Number(repeated);
    for (let i = 0; i < rows.length; i++) {
      const index = (start + i) % rows.length;
      if (!rows[index].disabled && rows[index].label.trim().toLocaleLowerCase().startsWith(query)) {
        setActive(index);
        return;
      }
    }
  }

  trigger.addEventListener('click', () => open ? hide() : show(), {signal});
  trigger.addEventListener('keydown', event => {
    const {key} = event;
    if (key === 'Tab') {
      if (open && active >= 0) choose(active);
    } else if (key === 'Escape' && open) {
      event.preventDefault();
      event.stopPropagation();
      hide();
    } else if (key === 'ArrowDown' || key === 'ArrowUp') {
      event.preventDefault();
      if (open) move(key === 'ArrowDown' ? 1 : -1);
      else show();
    } else if (key === 'Home' || key === 'End') {
      event.preventDefault();
      show();
      move(key === 'Home' ? 1 : -1, key === 'Home' ? -1 : rows.length);
    } else if (key === 'Enter' || key === ' ') {
      event.preventDefault();
      if (!open) show();
      else if (active >= 0) choose(active);
    } else if (key.length === 1 && key !== ' ' && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      show();
      typeAhead(key.toLocaleLowerCase());
    }
  }, {signal});
  trigger.addEventListener('blur', () => hide(), {signal});
  menu.addEventListener('pointerdown', event => {
    if (event.target.closest('.om-select-option')) event.preventDefault();
  }, {signal});
  menu.addEventListener('pointermove', event => {
    const row = event.target.closest('.om-select-option');
    if (open && row && event.pointerType !== 'touch') setActive(Number(row.dataset.index));
  }, {signal});
  menu.addEventListener('click', event => {
    const row = event.target.closest('.om-select-option');
    if (!open || !row) return;
    trigger.focus({preventScroll: true});
    choose(Number(row.dataset.index));
  }, {signal});
  window.addEventListener('pointerdown', event => {
    if (!event.composedPath().includes(host)) hide();
  }, {signal});
  function reposition() {
    if (!open) return;
    layoutMenu();
    height.goal = menuHeight;
    if (active >= 0) setActive(active, true);
    driver.play();
  }
  window.addEventListener('resize', reposition, {signal});
  window.visualViewport?.addEventListener('resize', reposition, {signal});
  if (!inline) select.closest('dialog')?.addEventListener('scroll', reposition, {passive:true, signal});
  for (const label of labels) {
    label.addEventListener('click', event => {
      if (event.target.closest('button, a, input, select, textarea')) return;
      event.preventDefault();
      trigger.focus();
    }, {signal});
  }
  select.addEventListener('focus', () => trigger.focus(), {signal});
  select.addEventListener('input', () => refresh(), {signal});
  select.addEventListener('change', () => refresh(), {signal});
  select.addEventListener('invalid', event => {
    event.preventDefault();
    trigger.setAttribute('aria-invalid', 'true');
    trigger.setAttribute('aria-describedby', [originalDescription, error.id].filter(Boolean).join(' '));
    error.textContent = select.validationMessage;
    const anchor = document.activeElement?.getAttribute('aria-invalid') === 'true';
    if (!anchor) trigger.focus();
  }, {signal});
  select.form?.addEventListener('reset', () => {
    cancelAnimationFrame(resetFrame);
    resetFrame = requestAnimationFrame(() => refresh());
  }, {signal});
  reducedMotion.addEventListener('change', () => {
    if (!reducedMotion.matches) return;
    clearTimeout(closeTimer);
    sheetMotion?.cancel();
    if (!open) height.goal = alpha.goal = angle.goal = highlightAlpha.goal = 0;
    animateRows(open);
    driver.play();
  }, {signal});
  const observer = new MutationObserver(() => refresh());
  observer.observe(select, {subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ['disabled', 'selected', 'label', 'hidden', 'required']});
  const fieldset = select.closest('fieldset');
  if (fieldset) observer.observe(fieldset, {attributes: true, attributeFilter: ['disabled']});
  refresh(false);

  return {
    refresh,
    close:hide,
    destroy() {
      listeners.abort();
      observer.disconnect();
      sheetMotion?.cancel();
      driver.stop();
      for (const timer of rowTimers) clearTimeout(timer);
      clearTimeout(closeTimer);
      cancelAnimationFrame(resetFrame);
      for (const [label, generated] of generatedIds) {
        if (label.id === generated) label.removeAttribute('id');
      }
      for (const [attribute, value] of [['tabindex', previousTabIndex], ['aria-hidden', previousAriaHidden]]) {
        if (value === null) select.removeAttribute(attribute);
        else select.setAttribute(attribute, value);
      }
      select.classList.remove('om-select-native');
      host.remove();
    },
  };
}

window.OrrerySpawnControls = {mountDropdown, mountScrubber, mountToggle};
})();
