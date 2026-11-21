(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const entry = $('mobile-new-agent'), sheet = $('spawn-sheet'), title = $('spawn-title');
  const editor = $('spawn-editor'), review = $('spawn-review'), result = $('spawn-result'), notice = $('spawn-notice');
  const task = $('spawn-task'), role = $('spawn-role'), model = $('spawn-model'), harness = $('spawn-harness');
  const directory = $('spawn-directory'), effort = $('spawn-effort'), reviewButton = $('spawn-review-button');
  const settings = $('spawn-settings'), settingsToggle = $('spawn-settings-toggle');
  const computer = $('spawn-computer'), browser = $('spawn-browser');
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
    reviewButton.disabled = !task.value.trim() || !next || !catalog?.directories.length
      || (next.effort_required && !effort.value);
  }

  function updateRoute() {
    const next = chosenRoute();
    if (!next) return;
    const provider = catalog.models[Number(model.value)].provider;
    const key = provider + '/' + next.key;
    if (route !== key) {
      const on = catalog.configurable && next.key !== 'dsh';
      for (const control of [computer, browser]) control.setAttribute('aria-checked', String(on));
    }
    route = key;
    const previous = effort.value;
    const entries = next.efforts.map(value => [value, effortLabel(value)]);
    const first = next.effort_required ? 'Choose effort' : 'Default';
    fill(effort, [['', first], ...entries]);
    if (next.efforts.includes(previous)) effort.value = previous;
    $('spawn-effort-field').hidden = !entries.length;
    canReview();
  }

  function updateModel() {
    const choices = catalog.models[Number(model.value)].harnesses;
    const previous = harness.value;
    fill(harness, choices.map(item => [item.id, item.label]));
    const selected = choices.find(item => item.id === previous) || choices.find(item => item.default) || choices[0];
    harness.value = selected.id;
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
      const restoreModel = catalog.models.findIndex(item => item.provider === previousModel?.provider
        && item.model === previousModel.model && item.url_index === previousModel.url_index);
      const restoreDir = catalog.directories.findIndex(item => item.path === previousDir);
      const defaultModel = catalog.models.findIndex(item => item.default);
      model.value = String(restoreModel >= 0 ? restoreModel : Math.max(0, defaultModel));
      if (restoreDir >= 0) directory.value = String(restoreDir);
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
    if (receipt) checkReceipt();
    else loadCatalog();
  };
  $('spawn-close').onclick = () => sheet.close();
  sheet.addEventListener('close', () => {catalogRequest?.abort();clearTimeout(timer);});
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(timer);
    else if (!submitting) checkReceipt();
  });
  settingsToggle.onclick = () => {
    settings.hidden = !settings.hidden;
    settingsToggle.setAttribute('aria-expanded', String(!settings.hidden));
  };
  for (const control of [computer, browser]) {
    control.onclick = () => control.setAttribute('aria-checked', String(control.getAttribute('aria-checked') !== 'true'));
  }
  model.onchange = updateModel;
  harness.onchange = updateRoute;
  effort.onchange = canReview;
  task.oninput = canReview;
  editor.onsubmit = reviewSelection;
  $('spawn-edit').onclick = () => {show('editor');reviewButton.focus({preventScroll:true});};
  $('spawn-confirm').onclick = launch;
  document.addEventListener('orrery-mobile-input-ready', enabled);
  enabled();
})();
