(() => {
  'use strict';
  if (location.pathname === '/reader-pane') return;
  const $ = id => document.getElementById(id);
  const entry = $('mobile-new-agent'), sheet = $('spawn-sheet'), title = $('spawn-title');
  const editor = $('spawn-editor'), review = $('spawn-review'), result = $('spawn-result'), notice = $('spawn-notice');
  const task = $('spawn-task'), role = $('spawn-role'), model = $('spawn-model'), harness = $('spawn-harness');
  const directory = $('spawn-directory'), effort = $('spawn-effort'), reviewButton = $('spawn-review-button');
  const settings = $('spawn-settings'), modelChip = $('spawn-model-chip');
  const modelName = $('spawn-model-name'), effortMark = $('spawn-model-effort');
  const computer = $('spawn-computer'), browser = $('spawn-browser');
  const mobileControls = window.OrrerySpawnControls;
  const selects = new Map([
    [model, mobileControls?.mountDropdown(model, {inline:true})],
    [harness, mobileControls?.mountDropdown(harness, {inline:true})],
    [directory, mobileControls?.mountDropdown(directory, {selectedLabel:option => option?.dataset.shortLabel})],
  ]);
  const effortRange = $('spawn-effort-range'), effortControl = $('spawn-effort-control');
  const tools = $('spawn-tools');
  const scrubber = mobileControls?.mountScrubber(effortRange, {
    output: $('spawn-effort-value'),
    formatValue: index => effort.options[index]?.label,
  });
  if (scrubber) {
    effort.hidden = true;
    effortControl.hidden = false;
    effortRange.addEventListener('input', () => {
      effort.selectedIndex = effortRange.valueAsNumber;
      canReview();
    });
  }
  const toggleMotion = [computer, browser].map(button => mobileControls?.mountToggle(button));

  function syncEffort() {
    if (!scrubber) return;
    effortRange.max = Math.max(0, effort.length - 1);
    effortRange.value = Math.max(0, effort.selectedIndex);
    effortRange.disabled = effort.length < 2;
    scrubber.sync();
  }

  function dismissToolHelp() {
    for (const tool of tools.children) delete tool.dataset.hint;
  }

  const receiptKey = 'orrery-mobile-launch-receipt';
  let catalog, route, selection, receipt = '', submitting = false, timer = 0, catalogRequest;

  try {
    const saved = sessionStorage.getItem(receiptKey);
    if (/^[a-f0-9]{32}$/.test(saved || '')) receipt = saved;
  } catch (_) {}

  function saveReceipt(value) {
    receipt = value;
    try {
      if (value) sessionStorage.setItem(receiptKey, value);
      else sessionStorage.removeItem(receiptKey);
    } catch (_) {}
  }

  function show(view, message = '') {
    editor.hidden = view !== 'editor';
    review.hidden = view !== 'review';
    result.hidden = view !== 'result';
    notice.hidden = !message;
    notice.textContent = message;
    title.textContent = view === 'review' ? 'Review launch' : 'New agent';
    if (view !== 'editor') {
      if (settings.open) settings.close();
      for (const control of selects.values()) control?.close();
      dismissToolHelp();
    }
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      credentials:'same-origin', cache:'no-store', referrerPolicy:'no-referrer', ...options,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'launch_unavailable');
    return data;
  }

  function fill(select, choices) {
    const options = choices.map(([value, label]) => new Option(label, value));
    select.replaceChildren(...options);
  }

  function effortLabel(value) {
    return value ? value.charAt(0).toUpperCase() + value.slice(1) : 'Default';
  }

  function modelChoices(models) {
    const groups = new Map();
    for (const [index, item] of models.entries()) {
      if (!groups.has(item.provider)) {
        const group = document.createElement('optgroup');
        group.label = item.provider_label;
        groups.set(item.provider, group);
      }
      const endpoint = Number.isInteger(item.url_index) ? ' (URL ' + (item.url_index + 1) + ')' : '';
      groups.get(item.provider).append(new Option(item.model + endpoint, index));
    }
    model.replaceChildren(...groups.values());
  }

  function chosenRoute() {
    return catalog?.models[Number(model.value)]?.harnesses.find(item => item.id === harness.value);
  }

  function enabled() {
    const available = window.OrreryMobileTerminal?.inputAvailable?.() === true;
    entry.hidden = !available;
    return available;
  }

  function canReview() {
    const next = chosenRoute();
    const name = model.selectedOptions[0]?.label || 'Model';
    modelName.textContent = name;
    effortMark.hidden = !next?.efforts.length;
    const value = next && (effort.value || next.default_effort);
    const level = next?.efforts.indexOf(value) ?? -1;
    const filled = level < 0 ? 0 : 1 + Math.round(4 * level / Math.max(1, next.efforts.length - 1));
    [...effortMark.children].forEach((mark, index) => mark.classList.toggle('active', index < filled));
    modelChip.title = level < 0 ? name : name + ' · ' + effortLabel(value);
    modelChip.setAttribute('aria-label', 'Model settings: ' + modelChip.title);
    reviewButton.disabled = !task.value.trim() || !next || !catalog?.directories.length
      || (next.effort_required && !effort.value);
  }

  function updateRoute() {
    const next = chosenRoute();
    if (!next) return;
    const provider = catalog.models[Number(model.value)].provider;
    const key = provider + '/' + next.key;
    if (route !== key) {
      dismissToolHelp();
      const on = catalog.configurable && next.key !== 'dsh';
      for (const control of [computer, browser]) control.setAttribute('aria-checked', String(on));
      for (const motion of toggleMotion) motion?.sync(false);
    }
    route = key;
    const previous = effort.value;
    const entries = next.efforts.map(value => [value, effortLabel(value)]);
    const first = next.effort_required ? 'Choose effort' : 'Default';
    fill(effort, [['', first], ...entries]);
    if (next.efforts.includes(previous)) effort.value = previous;
    $('spawn-effort-field').hidden = !entries.length;
    syncEffort();
    canReview();
    if (settings.open) positionModelSheet();
  }

  function updateModel() {
    const choices = catalog.models[Number(model.value)].harnesses;
    const previous = harness.value;
    fill(harness, choices.map(item => [item.id, item.label]));
    const selected = choices.find(item => item.id === previous) || choices.find(item => item.default) || choices[0];
    harness.value = selected.id;
    selects.get(harness)?.refresh(false);
    updateRoute();
  }

  function dismissResult() {
    saveReceipt('');
    task.value = '';
    role.value = '';
    sheet.close();
  }

  function resultButton(label, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'spawn-secondary';
    button.textContent = label;
    button.onclick = handler;
    result.append(button);
  }

  function paintResult(data) {
    const {state, name} = data;
    result.replaceChildren();
    if (state === 'ready') {
      show('result', name + ' is ready.');
      resultButton('Open Reading View', () => {
        dismissResult();
        window.OrreryMobileTerminal.open(name, window.OrreryMobileIdentity?.forName(name), 'conversation');
      });
      resultButton('Done', dismissResult);
    } else if (state === 'failed') {
      saveReceipt('');
      show('result', 'Launch failed.');
      resultButton('Edit', loadCatalog);
    } else if (state === 'submitting' || state === 'launching') {
      show('result', state === 'launching' ? 'Starting ' + name + '…' : 'Submitting launch…');
      if (sheet.open && !document.hidden && !submitting) timer = setTimeout(checkReceipt, 2000);
    } else {
      show('result', 'Launch outcome unknown. Check the graph before trying again.');
      resultButton('Check launch', checkReceipt);
      resultButton('Done', dismissResult);
    }
  }

  async function checkReceipt() {
    clearTimeout(timer);
    if (!receipt || !sheet.open) return;
    const id = receipt;
    try {
      const state = await request('/view/spawn/status?receipt=' + id);
      if (receipt === id && sheet.open) paintResult(state);
    } catch (_) {
      if (receipt === id && sheet.open) paintResult({state:'unknown'});
    }
  }

  async function loadCatalog() {
    catalogRequest?.abort();
    const controller = new AbortController();
    catalogRequest = controller;
    result.replaceChildren();
    show('result', 'Loading launch options…');
    try {
      const next = await request('/view/spawn', {signal:controller.signal});
      if (catalogRequest !== controller || !sheet.open) return;
      if (!next.models?.length || !next.directories?.length) {
        show('result', 'No available models or launch directories.');
        resultButton('Retry', loadCatalog);
        return;
      }
      const previousModel = catalog?.models[Number(model.value)];
      const previousDir = catalog?.directories[Number(directory.value)]?.path;
      catalog = next;
      modelChoices(catalog.models);
      fill(directory, catalog.directories.map((item, index) => [index, item.label]));
      for (const [index, option] of [...directory.options].entries()) {
        const path = catalog.directories[index].path;
        option.dataset.shortLabel = path.split('/').filter(Boolean).at(-1) || path;
      }
      const restoreModel = catalog.models.findIndex(item => item.provider === previousModel?.provider
        && item.model === previousModel.model && item.url_index === previousModel.url_index);
      const restoreDir = catalog.directories.findIndex(item => item.path === previousDir);
      const defaultModel = catalog.models.findIndex(item => item.default);
      model.value = String(restoreModel >= 0 ? restoreModel : Math.max(0, defaultModel));
      if (restoreDir >= 0) directory.value = String(restoreDir);
      selects.get(model)?.refresh(false);
      selects.get(directory)?.refresh(false);
      $('spawn-tools').hidden = !catalog.configurable;
      $('spawn-role-field').hidden = !catalog.configurable;
      updateModel();
      show('editor');
    } catch (error) {
      if (catalogRequest !== controller || !sheet.open) return;
      const disabled = error.message === 'launch_disabled';
      if (disabled) entry.hidden = true;
      show('result', disabled ? 'Launch is unavailable while Send from phone is off.' : 'Launch options unavailable.');
      if (!disabled) resultButton('Retry', loadCatalog);
    }
  }

  function summaryValue(label, icon, value) {
    const chip = document.createElement('div');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const use = document.createElementNS(svg.namespaceURI, 'use');
    svg.setAttribute('class', 'mobile-icon');
    svg.setAttribute('aria-hidden', 'true');
    use.setAttribute('href', '#icon-' + icon);
    svg.append(use);
    chip.className = 'spawn-value';
    chip.setAttribute('aria-label', label + ': ' + value);
    chip.title = label + ': ' + value;
    chip.append(svg, document.createTextNode(value));
    return chip;
  }

  function reviewSelection(event) {
    event.preventDefault();
    if (reviewButton.disabled) return;
    const route = chosenRoute();
    const selectedDirectory = catalog.directories[Number(directory.value)];
    const checked = control => control.getAttribute('aria-checked') === 'true';
    selection = {
      catalog:catalog.catalog, model:Number(model.value), harness:harness.value, directory:Number(directory.value),
      task:task.value.trim(), role:catalog.configurable ? role.value.trim() : '',
      effort:effort.value || null, computer:catalog.configurable && checked(computer),
      browser:catalog.configurable && checked(browser),
    };
    const labels = [
      summaryValue('Model and harness', 'launch-model', model.selectedOptions[0].textContent + ' with ' + route.label),
      summaryValue('Launch directory', 'launch-folder', selectedDirectory.path),
    ];
    if (route.efforts.length) labels.push(summaryValue('Effort', 'working', effortLabel(selection.effort || route.default_effort)));
    if (selection.role) labels.push(summaryValue('Role', 'agents', selection.role));
    if (catalog.configurable) {
      labels.push(summaryValue('Computer use', 'launch-computer', selection.computer ? 'On' : 'Off'));
      labels.push(summaryValue('Browser', 'launch-browser', selection.browser ? 'On' : 'Off'));
    }
    $('spawn-summary').replaceChildren(...labels);
    $('spawn-task-copy').textContent = selection.task;
    show('review');
    $('spawn-edit').focus({preventScroll:true});
  }

  async function launch() {
    if (!selection || submitting || receipt) return;
    submitting = true;
    const id = [...crypto.getRandomValues(new Uint8Array(16))].map(byte => byte.toString(16).padStart(2, '0')).join('');
    saveReceipt(id);
    paintResult({state:'submitting'});
    let state;
    try {
      state = await request('/view/spawn', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...selection, receipt:id}),
      });
    } catch (error) {
      if (error.message === 'catalog_expired' || error.message === 'invalid_task' || error.message === 'invalid_launch') {
        saveReceipt('');
        show('result', 'Review the launch options again.');
        resultButton('Edit', loadCatalog);
        return;
      }
      state = {state:'unknown'};
    } finally {
      submitting = false;
      selection = null;
    }
    if (sheet.open) paintResult(state);
  }

  entry.onclick = () => {
    if (!enabled()) return;
    sheet.showModal();
    resizeLaunch();
    if (receipt) checkReceipt();
    else loadCatalog();
  };
  $('spawn-close').onclick = () => sheet.close();
  sheet.addEventListener('close', () => {
    if (settings.open) settings.close();
    catalogRequest?.abort();
    clearTimeout(timer);
    for (const control of selects.values()) control?.close();
    dismissToolHelp();
  });
  sheet.addEventListener('pointerdown', event => {
    if (!tools.contains(event.target)) dismissToolHelp();
  });
  tools.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const tool = event.target.closest('.spawn-tool');
    if (!tool || getComputedStyle(tool.querySelector('.spawn-tool-help')).display === 'none') return;
    event.preventDefault();
    dismissToolHelp();
    tool.dataset.hint = 'dismissed';
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(timer);
    else if (!submitting) checkReceipt();
  });
  function positionModelSheet() {
    if (!settings.open) return;
    const anchor = modelChip.getBoundingClientRect();
    const {width, height} = settings.getBoundingClientRect();
    const viewport = window.visualViewport;
    const visibleLeft = viewport?.offsetLeft ?? 0;
    const visibleRight = visibleLeft + (viewport?.width ?? innerWidth);
    const visibleTop = viewport?.offsetTop ?? 0;
    const visibleBottom = visibleTop + (viewport?.height ?? innerHeight);
    const left = Math.max(visibleLeft + 12, Math.min(anchor.left, visibleRight - width - 12));
    const top = Math.max(visibleTop + 12, Math.min(anchor.bottom + 8, visibleBottom - height - 12));
    settings.style.setProperty('--spawn-model-left', left + 'px');
    settings.style.setProperty('--spawn-model-top', top + 'px');
    return anchor;
  }
  new ResizeObserver(positionModelSheet).observe(settings);

  modelChip.onclick = () => {
    dismissToolHelp();
    settings.showModal();
    modelChip.setAttribute('aria-expanded', 'true');
    const anchor = positionModelSheet();
    if (matchMedia('(max-width: 720px), (prefers-reduced-motion: reduce)').matches || !settings.animate) return;
    const {left, right, top, bottom} = settings.getBoundingClientRect();
    const collapsed = `inset(${anchor.top - top}px ${right - anchor.right}px ${bottom - anchor.bottom}px ${anchor.left - left}px round 99px)`;
    const easing = getComputedStyle(settings).getPropertyValue('--spawn-spring').trim();
    settings.animate([{clipPath:collapsed}, {clipPath:'inset(0 round 16px)'}], {duration:520, easing});
  };
  $('spawn-settings-close').onclick = () => settings.close();
  settings.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    event.preventDefault();
    settings.close();
  });
  settings.addEventListener('cancel', event => {
    event.preventDefault();
    settings.close();
  });
  settings.addEventListener('click', event => {
    if (event.target !== settings) return;
    const {left, right, top, bottom} = settings.getBoundingClientRect();
    if (event.clientX < left || event.clientX > right || event.clientY < top || event.clientY > bottom) settings.close();
  });
  settings.addEventListener('close', () => {
    selects.get(model)?.close();
    selects.get(harness)?.close();
    modelChip.setAttribute('aria-expanded', 'false');
    if (sheet.open && !editor.hidden && !sheet.contains(document.activeElement)) modelChip.focus({preventScroll:true});
  });
  function resizeLaunch() {
    if (!sheet.open) return;
    const viewport = window.visualViewport;
    const height = viewport?.height ?? innerHeight;
    const bottom = Math.max(0, innerHeight - height - (viewport?.offsetTop ?? 0));
    for (const dialog of [sheet, settings]) {
      dialog.style.setProperty('--spawn-visible-height', height + 'px');
      dialog.style.setProperty('--spawn-visible-bottom', bottom + 'px');
    }
    if (settings.open) positionModelSheet();
  }
  window.addEventListener('resize', resizeLaunch);
  window.visualViewport?.addEventListener('resize', resizeLaunch);
  window.visualViewport?.addEventListener('scroll', resizeLaunch);
  for (const control of [computer, browser]) {
    const tool = control.closest('.spawn-tool');
    control.addEventListener('blur', () => {delete tool.dataset.hint;});
    tool.addEventListener('pointerleave', event => {
      if (event.pointerType === 'mouse') delete tool.dataset.hint;
    });
    control.onclick = event => {
      control.setAttribute('aria-checked', String(control.getAttribute('aria-checked') !== 'true'));
      dismissToolHelp();
      if (event.pointerType === 'touch' || matchMedia('(hover: none)').matches) tool.dataset.hint = 'shown';
    };
  }
  model.onchange = updateModel;
  harness.onchange = updateRoute;
  effort.onchange = () => {syncEffort();canReview();};
  task.oninput = canReview;
  editor.onsubmit = reviewSelection;
  $('spawn-edit').onclick = () => {show('editor');reviewButton.focus({preventScroll:true});};
  $('spawn-confirm').onclick = launch;
  document.addEventListener('orrery-mobile-input-ready', enabled);
  enabled();
})();
