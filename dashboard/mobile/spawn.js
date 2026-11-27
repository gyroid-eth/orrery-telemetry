(() => {
  'use strict';
  if (location.pathname === '/reader-pane') return;
  const $ = id => document.getElementById(id);
  const entry = $('mobile-new-agent'), sheet = $('spawn-sheet'), title = $('spawn-title');
  const editor = $('spawn-editor'), review = $('spawn-review'), result = $('spawn-result'), notice = $('spawn-notice');
  const task = $('spawn-task'), role = $('spawn-role'), harness = $('spawn-harness');
  const providerTabs = $('spawn-provider-tabs'), modelSearch = $('spawn-model-search'), modelList = $('spawn-model-list');
  const modelNotice = $('spawn-model-notice'), modelEmpty = $('spawn-model-empty');
  const directory = $('spawn-directory'), effort = $('spawn-effort'), reviewButton = $('spawn-review-button');
  const settings = $('spawn-settings'), modelChip = $('spawn-model-chip');
  const modelName = $('spawn-model-name'), effortMark = $('spawn-model-effort');
  const computer = $('spawn-computer'), browser = $('spawn-browser');
  const computerTool = computer.closest('.spawn-tool');
  const browserTool = browser.closest('.spawn-tool');
  const computerMenu = $('spawn-computer-menu');
  const browserMenu = $('spawn-browser-menu');
  const computerLabels = {off:'Off', view:'View', control:'Control'};
  const browserLabels = {off:'Off', temporary:'Temporary Chrome', personal:'My Chrome'};
  let computerMode = 'off';
  let browserMode = 'off';
  function modeChoices(menu, control, labels, select) {
    return Object.entries(labels).map(([mode, label]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = label;
      button.onclick = () => {
        select(mode);
        menu.hidePopover();
        control.focus({preventScroll:true});
      };
      menu.append(button);
      return [mode, button];
    });
  }
  const computerChoices = modeChoices(computerMenu, computer, computerLabels, mode => {
    computerMode = mode;
    syncComputer(chosenRoute());
  });
  const browserChoices = modeChoices(browserMenu, browser, browserLabels, mode => {
    browserMode = mode;
    syncBrowser(chosenRoute());
  });
  const mobileControls = window.OrrerySpawnControls;
  const selects = new Map([
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
  const providerIcons = new Map([['claude', 'anthropic'], ['codex', 'openai'], ['gemini', 'google'], ['deepseek', 'deepseek']]);
  const providerOrder = [...providerIcons.keys(), 'local'];
  const failureReasons = new Map([
    ['unsupported_tools', 'Turn off optional tools or choose another harness.'],
    ['browser_setup', 'Turn off Browser or set up Chrome on your computer.'],
    ['model_unavailable', 'Choose another model or harness.'],
    ['launch_directory', 'Choose another launch folder.'],
    ['check_graph', 'Check the graph before trying again.'],
  ]);
  const modelGroups = new Map();
  let modelIndex = -1;
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
    if (view === 'editor') {
      canReview();
    } else {
      if (computerMenu.matches(':popover-open')) computerMenu.hidePopover();
      if (browserMenu.matches(':popover-open')) browserMenu.hidePopover();
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

  const selectedModel = () => catalog?.models[modelIndex];
  const providerFor = item => item.locality === 'local' ? 'local' : item.provider;
  const modelLabel = item => item.model + (Number.isInteger(item.url_index) ? ' (URL ' + (item.url_index + 1) + ')' : '');
  const modelSource = item => providerFor(item) === 'local' && item.provider !== 'local' ? item.provider_label : '';

  function renderModels() {
    const group = modelGroups.get(providerFor(selectedModel()));
    const query = modelSearch.value.trim().toLowerCase();
    const list = document.createDocumentFragment();
    for (const index of group.indices) {
      const item = catalog.models[index], name = modelLabel(item), source = modelSource(item);
      if (query && !`${name} ${source}`.toLowerCase().includes(query)) continue;
      const row = document.createElement('label');
      row.className = 'spawn-model-choice';
      row.title = source ? `${name} — ${source}` : name;
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'spawn-model-choice';
      input.value = String(index);
      input.checked = index === modelIndex;
      input.addEventListener('change', () => {
        modelIndex = index;
        updateModel();
      });
      const label = document.createElement('span');
      label.className = 'spawn-model-label';
      label.textContent = name;
      row.append(input, label);
      if (source) {
        const detail = document.createElement('span');
        detail.className = 'spawn-model-source';
        detail.textContent = source;
        row.append(detail);
      }
      list.append(row);
    }
    modelList.setAttribute('aria-label', group.label + ' models');
    modelEmpty.hidden = list.childElementCount !== 0;
    modelList.replaceChildren(list);
    modelList.scrollTop = 0;
  }

  function renderProviders() {
    modelGroups.clear();
    for (const [index, item] of catalog.models.entries()) {
      const key = providerFor(item);
      if (!modelGroups.has(key)) modelGroups.set(key, {label:key === 'local' ? 'Local' : item.provider_label, indices:[]});
      modelGroups.get(key).indices.push(index);
    }
    const order = [...providerOrder.filter(key => modelGroups.has(key)),
      ...[...modelGroups.keys()].filter(key => !providerOrder.includes(key))];
    const current = providerFor(selectedModel());
    const fragment = document.createDocumentFragment();
    for (const key of order) {
      const group = modelGroups.get(key);
      const tab = document.createElement('button');
      tab.className = 'spawn-provider-tab';
      tab.type = 'button';
      tab.title = group.label;
      tab.setAttribute('aria-label', group.label);
      tab.setAttribute('aria-controls', modelList.id);
      tab.setAttribute('aria-pressed', String(key === current));
      if (providerIcons.has(key)) {
        const img = document.createElement('img');
        img.alt = '';
        img.src = '/assets/' + providerIcons.get(key) + '.svg';
        tab.append(img);
      } else if (key === 'local') {
        tab.append(spawnIcon('launch-local'));
      } else {
        tab.textContent = group.label;
      }
      tab.onclick = () => {
        if (key === providerFor(selectedModel())) return;
        providerTabs.querySelector('[aria-pressed=true]')?.setAttribute('aria-pressed', 'false');
        tab.setAttribute('aria-pressed', 'true');
        modelIndex = group.indices.find(index => catalog.models[index].default) ?? group.indices[0];
        modelSearch.value = '';
        renderModels();
        updateModel();
      };
      fragment.append(tab);
    }
    providerTabs.replaceChildren(fragment);
    modelSearch.value = '';
    renderModels();
  }

  function chosenRoute() {
    return selectedModel()?.harnesses.find(item => item.id === harness.value);
  }

  function browserAvailable(next = chosenRoute()) {
    return catalog?.configurable && next && next.key !== 'antigravity' &&
      (!['dsh', 'hermes'].includes(next.key) || catalog.directories[Number(directory.value)]?.native_browser === true);
  }

  function computerModes(next = chosenRoute()) {
    return catalog?.configurable && Array.isArray(next?.computer_modes)
      ? next.computer_modes : null;
  }

  function syncComputer(next, reset = false) {
    const modes = computerModes(next);
    computerTool.hidden = next?.key === 'antigravity' && !modes;
    if (!modes) {
      if (computerMenu.matches(':popover-open')) computerMenu.hidePopover();
      if ('computerMode' in computer.dataset) {
        delete computer.dataset.computerMode;
        computer.setAttribute('role', 'switch');
        computer.setAttribute('aria-label', 'Computer use');
        computer.setAttribute('aria-describedby', 'spawn-computer-help');
        for (const attr of ['aria-expanded', 'aria-controls', 'aria-disabled', 'title']) computer.removeAttribute(attr);
        reset = true;
      }
      if (reset) {
        computer.setAttribute('aria-checked', String(!tools.hidden && next.key !== 'dsh'));
        toggleMotion[0]?.sync(false);
      }
      return;
    }
    if (reset) {
      computerMode = next.key === 'dsh' || next.key === 'antigravity' || next.computer_use_transport === 'orrery-screen' ? 'off' : 'control';
      if (computerMenu.matches(':popover-open')) computerMenu.hidePopover();
    }
    if (!modes.includes(computerMode)) computerMode = modes[0] || 'off';
    computer.dataset.computerMode = computerMode;
    for (const attr of ['role', 'aria-checked', 'aria-describedby']) computer.removeAttribute(attr);
    computer.setAttribute('aria-controls', computerMenu.id);
    computer.setAttribute('aria-expanded', String(computerMenu.matches(':popover-open')));
    computer.setAttribute('aria-disabled', String(!modes.length || Boolean(next.computer_use_reason)));
    computer.title = next.computer_use_reason || 'Computer use: ' + (modes.length ? computerLabels[computerMode] : 'Unavailable');
    computer.setAttribute('aria-label', computer.title);
    for (const [mode, button] of computerChoices) {
      button.hidden = !modes.includes(mode);
      button.setAttribute('aria-pressed', String(mode === computerMode));
    }
  }

  function browserModes(next = chosenRoute()) {
    return catalog?.configurable && Array.isArray(next?.browser_modes)
      ? next.browser_modes : null;
  }

  function syncBrowser(next, reset = false) {
    const modes = browserModes(next);
    if (modes) {
      if (reset || !modes.includes(browserMode)) browserMode = modes.includes('personal') ? 'personal' : 'off';
      browserTool.hidden = false;
      browser.dataset.browserMode = browserMode;
      browser.removeAttribute('role');
      browser.removeAttribute('aria-checked');
      browser.removeAttribute('aria-describedby');
      browser.setAttribute('aria-controls', browserMenu.id);
      browser.setAttribute('aria-expanded', String(browserMenu.matches(':popover-open')));
      browser.setAttribute('aria-disabled', String(!modes.length));
      const extra = next.launch_blocked ? 'Antigravity needs a parent agent and declared resources.'
        : !modes.length ? 'Browser modes are unavailable for this harness.' : '';
      browser.title = extra || 'Browser: ' + browserLabels[browserMode]
        + (modes.length < browserChoices.length ? '. Other modes need a compatible Chrome setup.' : '');
      browser.setAttribute('aria-label', browser.title);
      for (const [mode, button] of browserChoices) {
        button.hidden = !modes.includes(mode);
        button.setAttribute('aria-pressed', String(mode === browserMode));
      }
      if (!modes.length && browserMenu.matches(':popover-open')) browserMenu.hidePopover();
      return;
    }
    if ('browserMode' in browser.dataset) {
      delete browser.dataset.browserMode;
      browser.setAttribute('role', 'switch');
      browser.setAttribute('aria-label', 'Browser');
      browser.setAttribute('aria-describedby', 'spawn-browser-help');
      for (const attr of ['aria-expanded', 'aria-controls', 'aria-disabled', 'title']) browser.removeAttribute(attr);
      if (browserMenu.matches(':popover-open')) browserMenu.hidePopover();
      reset = true;
    }
    const available = Boolean(browserAvailable(next));
    if (reset || browserTool.hidden === available) {
      browser.setAttribute('aria-checked', String(available && next.key !== 'dsh'));
      toggleMotion[1]?.sync(false);
    }
    browserTool.hidden = !available;
  }

  function enabled() {
    const available = window.OrreryMobileTerminal?.inputAvailable?.() === true;
    entry.hidden = !available;
    return available;
  }

  function canReview() {
    const next = chosenRoute();
    const item = selectedModel();
    const name = item ? modelLabel(item) : 'Model';
    modelName.textContent = name;
    effortMark.hidden = !next?.efforts.length;
    const value = next && (effort.value || next.default_effort);
    const level = next?.efforts.indexOf(value) ?? -1;
    const filled = level < 0 ? 0 : 1 + Math.round(4 * level / Math.max(1, next.efforts.length - 1));
    [...effortMark.children].forEach((mark, index) => mark.classList.toggle('active', index < filled));
    modelChip.title = level < 0 ? name : name + ' · ' + effortLabel(value);
    modelChip.setAttribute('aria-label', 'Model settings: ' + modelChip.title);
    if (!editor.hidden) {
      const message = next?.launch_blocked ? 'Antigravity needs a parent agent and declared resources. Set these in the Telemetry dashboard.' : '';
      for (const destination of [notice, modelNotice]) {
        if (destination.textContent !== message) destination.textContent = message;
        destination.hidden = !message;
      }
    }
    reviewButton.disabled = !task.value.trim() || !next || next.launch_blocked || !catalog?.directories.length
      || (next.effort_required && !effort.value);
  }

  function updateRoute() {
    const next = chosenRoute();
    if (!next) return;
    const provider = selectedModel().provider;
    const key = provider + '/' + next.key;
    tools.hidden = !catalog.configurable ||
      (next.key === 'antigravity' && !computerModes(next)?.length && !browserModes(next)?.length);
    const changed = route !== key;
    if (changed) dismissToolHelp();
    syncComputer(next, changed);
    syncBrowser(next, changed);
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
    const choices = selectedModel().harnesses;
    const previous = harness.value;
    fill(harness, choices.map(item => [item.id, item.label]));
    const selected = choices.find(item => item.id === previous) || choices.find(item => item.default) || choices[0];
    harness.value = selected.id;
    $('spawn-harness-field').hidden = choices.length < 2;
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
      notice.append(document.createElement('br'), failureReasons.get(data.failure_reason) || 'Check the launch settings and try again.');
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
      const previousModel = selectedModel();
      const previousDir = catalog?.directories[Number(directory.value)]?.path;
      catalog = next;
      fill(directory, catalog.directories.map((item, index) => [index, item.label]));
      for (const [index, option] of [...directory.options].entries()) {
        const path = catalog.directories[index].path;
        option.dataset.shortLabel = path.split('/').filter(Boolean).at(-1) || path;
      }
      const restoreModel = catalog.models.findIndex(item => item.provider === previousModel?.provider
        && item.model === previousModel.model && item.url_index === previousModel.url_index);
      const restoreDir = catalog.directories.findIndex(item => item.path === previousDir);
      const defaultModel = catalog.models.findIndex(item => item.default);
      modelIndex = restoreModel >= 0 ? restoreModel : Math.max(0, defaultModel);
      if (restoreDir >= 0) directory.value = String(restoreDir);
      renderProviders();
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

  function spawnIcon(icon) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const use = document.createElementNS(svg.namespaceURI, 'use');
    svg.setAttribute('class', 'mobile-icon');
    svg.setAttribute('aria-hidden', 'true');
    use.setAttribute('href', '#icon-' + icon);
    svg.append(use);
    return svg;
  }

  function summaryValue(label, icon, value) {
    const chip = document.createElement('div');
    chip.className = 'spawn-value';
    chip.setAttribute('aria-label', label + ': ' + value);
    chip.title = label + ': ' + value;
    chip.append(spawnIcon(icon), document.createTextNode(value));
    return chip;
  }

  function reviewSelection(event) {
    event.preventDefault();
    if (reviewButton.disabled) return;
    const route = chosenRoute();
    const selectedDirectory = catalog.directories[Number(directory.value)];
    const checked = control => control.getAttribute('aria-checked') === 'true';
    const canUseBrowser = browserAvailable(route);
    const modes = browserModes(route);
    const screens = computerModes(route);
    selection = {
      catalog:catalog.catalog, model:modelIndex, harness:harness.value, directory:Number(directory.value),
      task:task.value.trim(), role:catalog.configurable ? role.value.trim() : '',
      effort:effort.value || null,
      computer:!tools.hidden && (screens?.includes(computerMode) ? computerMode : !screens && checked(computer)),
      ...(modes ? (modes.includes(browserMode) ? {browser_mode:browserMode} : {})
        : canUseBrowser || (!catalog.configurable && route.key !== 'antigravity')
          ? {browser:Boolean(canUseBrowser) && checked(browser)} : {}),
    };
    const item = selectedModel(), source = modelSource(item);
    const name = modelLabel(item) + (source ? ' on ' + source : '');
    const labels = [
      summaryValue('Model and harness', 'launch-model', name + ' with ' + route.label),
      summaryValue('Launch directory', 'launch-folder', selectedDirectory.path),
    ];
    if (route.efforts.length) labels.push(summaryValue('Effort', 'working', effortLabel(selection.effort || route.default_effort)));
    if (selection.role) labels.push(summaryValue('Role', 'agents', selection.role));
    if (!tools.hidden) {
      if (!computerTool.hidden) labels.push(summaryValue('Computer use', 'launch-computer',
        screens ? computerLabels[selection.computer] || 'Unavailable' : selection.computer ? 'On' : 'Off'));
      if (selection.browser_mode) labels.push(summaryValue('Browser', 'launch-browser', browserLabels[selection.browser_mode]));
      else if (!modes && canUseBrowser) labels.push(summaryValue('Browser', 'launch-browser', selection.browser ? 'On' : 'Off'));
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
    if (computerMenu.matches(':popover-open')) computerMenu.hidePopover();
    if (browserMenu.matches(':popover-open')) browserMenu.hidePopover();
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
    if (modelSearch.value) {
      modelSearch.value = '';
      renderModels();
    }
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
    selects.get(harness)?.close();
    modelChip.setAttribute('aria-expanded', 'false');
    if (sheet.open && !editor.hidden) modelChip.focus({preventScroll:true});
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
  for (const [control, menu, available] of [
    [computer, computerMenu, computerModes], [browser, browserMenu, browserModes],
  ]) {
    const tool = control.closest('.spawn-tool');
    control.addEventListener('blur', () => {delete tool.dataset.hint;});
    tool.addEventListener('pointerleave', event => {
      if (event.pointerType === 'mouse') delete tool.dataset.hint;
    });
    control.onclick = event => {
      if (available()) {
        if (menu.matches(':popover-open')) menu.hidePopover();
        else if (control.getAttribute('aria-disabled') !== 'true') {
          menu.showPopover({source:control});
          const anchor = control.getBoundingClientRect(), bounds = menu.getBoundingClientRect();
          menu.style.left = Math.max(12, Math.min(anchor.left, innerWidth - bounds.width - 12)) + 'px';
          menu.style.top = Math.max(12, anchor.top - bounds.height - 6) + 'px';
          menu.querySelector('[aria-pressed=true]:not([hidden])')?.focus({preventScroll:true});
        }
        return;
      }
      control.setAttribute('aria-checked', String(control.getAttribute('aria-checked') !== 'true'));
      dismissToolHelp();
      if (event.pointerType === 'touch' || matchMedia('(hover: none)').matches) tool.dataset.hint = 'shown';
    };
    menu.addEventListener('toggle', () => {
      if (available()) control.setAttribute('aria-expanded', String(menu.matches(':popover-open')));
    });
  }
  modelSearch.oninput = renderModels;
  harness.onchange = updateRoute;
  directory.onchange = () => syncBrowser(chosenRoute());
  effort.onchange = () => {syncEffort();canReview();};
  task.oninput = canReview;
  editor.onsubmit = reviewSelection;
  $('spawn-edit').onclick = () => {show('editor');reviewButton.focus({preventScroll:true});};
  $('spawn-confirm').onclick = launch;
  document.addEventListener('orrery-mobile-input-ready', enabled);
  enabled();
})();
