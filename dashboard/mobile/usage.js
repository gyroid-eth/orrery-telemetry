/* Header account usage and the corresponding read-only phone sheet. */
(() => {
  'use strict';
  if (document.documentElement.hasAttribute('data-reader-pane')) return;

  const strip = document.getElementById('usage-strip');
  const readings = document.getElementById('usage-readings');
  const readTime = document.getElementById('usage-read-time');
  const sheet = document.getElementById('usage-sheet');
  const details = document.getElementById('usage-details');
  const providers = {
    claude: ['Claude', 'anthropic'],
    codex: ['Codex', 'openai'],
    antigravity: ['Antigravity', 'google'],
    dsh: ['DeepSeek', 'deepseek'],
  };
  const clock = new Intl.DateTimeFormat([], {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
  const date = new Intl.DateTimeFormat([], {month: 'short', day: 'numeric'});
  const usd = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD'});
  const pollInterval = 60_000;
  let snapshot, receivedAt, request, nextRead = 0;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function logo(asset) {
    const image = element('img', 'usage-logo');
    image.src = '/assets/' + asset + '.svg';
    image.alt = '';
    return image;
  }

  function observationTime(stamp, now) {
    const value = new Date(stamp * 1000);
    const time = clock.format(value);
    return value.toDateString() === new Date(now * 1000).toDateString()
      ? time : date.format(value) + ' ' + time;
  }

  function duration(seconds) {
    if (seconds % 86400 === 0) return seconds / 86400 + 'd';
    if (seconds % 3600 === 0) return seconds / 3600 + 'h';
    return seconds % 60 === 0 ? seconds / 60 + 'm' : seconds + 's';
  }

  function resetLabel(at, now) {
    if (!at) return '';
    if (at <= now) return 'Reset passed';
    const minutes = Math.ceil((at - now) / 60);
    const hours = Math.floor(minutes / 60);
    const remaining = hours >= 24
      ? Math.floor(hours / 24) + 'd ' + hours % 24 + 'h'
      : hours ? hours + 'h ' + minutes % 60 + 'm' : minutes + 'm';
    return 'Resets in ' + remaining;
  }

  function state(reading, now) {
    if (reading.status === 'unavailable') return 'unavailable';
    return reading.status === 'stale' || now - reading.observed_at >= snapshot.fresh_seconds
      || reading.resets_at && reading.resets_at <= now ? 'stale' : 'available';
  }

  function observationLabel(state, stamp, now) {
    if (state === 'unavailable') return 'Unavailable';
    return (state === 'stale' ? 'Stale ' : 'Read ') + observationTime(stamp, now);
  }

  function applyState(node, reading, percent) {
    node.dataset.state = reading.state;
    if (percent !== null) node.dataset.usageTier = percent <= 20 ? 'low' : percent <= 50 ? 'warn' : 'good';
  }

  function renderWindow(window, now, providerReading) {
    const row = element('div', 'usage-window');
    const {remaining_percent: percent, window_seconds: seconds, observed_at: observed} = window;
    applyState(row, window, percent);
    const label = seconds ? duration(seconds) + ' window' : 'Limit';
    row.append(element('span', '', label),
      element('b', '', window.state === 'unavailable' ? 'Unavailable' : Math.round(percent) + '%'));
    if (window.state === 'unavailable') return row;

    const meter = element('progress');
    meter.max = 100;
    meter.value = percent;
    meter.setAttribute('aria-hidden', 'true');
    row.append(meter);
    const reset = resetLabel(window.resets_at, now);
    if (reset) row.append(element('small', '', reset));
    if (observed !== providerReading.observed_at || window.state !== providerReading.state) {
      row.append(element('small', '', observationLabel(window.state, observed, now)));
    }
    return row;
  }

  function renderProvider(entry, now, name, asset, mainReading, windows) {
    const section = element('section', 'usage-provider');
    const header = element('div', 'usage-provider-head');
    const heading = element('h3');
    heading.append(logo(asset), name);
    header.append(heading, element('span', '', observationLabel(mainReading.state, mainReading.observed_at, now)));
    section.append(header);
    if (entry.balance_usd !== undefined) {
      const balance = element('p', 'usage-balance');
      applyState(balance, mainReading, null);
      balance.append(element('b', '', usd.format(entry.balance_usd)), ' balance');
      if (!entry.balance_available) balance.append(element('small', '', 'Account unavailable'));
      section.append(balance);
    } else if (windows.some(window => window.state !== 'unavailable')) {
      const grid = element('div', 'usage-windows');
      grid.append(...windows.map(window => renderWindow(window, now, mainReading)));
      section.append(grid);
    }
    return section;
  }

  function render() {
    if (!snapshot?.providers.length) {
      const message = snapshot === undefined ? 'Loading usage…' : 'Usage unavailable';
      readings.textContent = message;
      readTime.hidden = true;
      details.replaceChildren(element('p', '', message));
      strip.setAttribute('aria-label', 'Provider usage. ' + message);
      return;
    }

    // Age readings from the relay's clock even if the phone has a clock offset.
    // Wall time also accounts for browsers where the monotonic clock sleeps.
    const elapsed = Math.max(Date.now() - receivedAt.wall, performance.now() - receivedAt.mono);
    const now = snapshot.server_time + elapsed / 1000;
    const items = [], sections = [], labels = [];
    let oldest = Infinity, stale = false;
    for (const entry of snapshot.providers) {
      const [name, asset] = providers[entry.provider];
      const windows = entry.windows.map(window => ({...window, state: state(window, now)}));
      const available = windows.filter(window => window.state !== 'unavailable');
      const binding = available.reduce((lowest, window) =>
        !lowest || window.remaining_percent < lowest.remaining_percent ? window : lowest, null);
      const main = binding || {...entry, state: state(entry, now)};
      const percent = binding?.remaining_percent ?? null;
      const value = main.state === 'unavailable' ? 'Unavailable'
        : percent !== null ? Math.round(percent) + '%' : usd.format(entry.balance_usd);
      const item = element('span', 'usage-compact');
      const label = name + ' ' + value + (percent === null ? '' : ' remaining');
      item.title = main.state === 'unavailable' ? label
        : label + '. ' + observationLabel(main.state, main.observed_at, now);
      if (entry.balance_available === false) item.title += '. Account unavailable';
      item.append(logo(asset), element('b', '', value));
      applyState(item, main, entry.balance_available === false ? 0 : percent);
      items.push(item);
      labels.push(item.title);
      if (main.state !== 'unavailable') {
        oldest = Math.min(oldest, main.observed_at);
        stale ||= main.state === 'stale';
      }
      const providerReading = available.length ? {
        observed_at: Math.min(...available.map(window => window.observed_at)),
        state: available.some(window => window.state === 'stale') ? 'stale' : 'available',
      } : main;
      sections.push(renderProvider(entry, now, name, asset, providerReading, windows));
    }
    readTime.hidden = oldest === Infinity;
    if (!readTime.hidden) {
      readTime.dataset.state = stale ? 'stale' : 'available';
      readTime.textContent = observationLabel(readTime.dataset.state, oldest, now);
    }
    readings.replaceChildren(...items);
    details.replaceChildren(...sections);
    strip.setAttribute('aria-label', 'Provider usage. ' + labels.join('. '));
  }

  async function refresh() {
    if (document.hidden || request) return;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 35_000);
    request = controller;
    nextRead = performance.now() + pollInterval;
    try {
      const response = await fetch('/view/quotas', {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('Usage unavailable');
      const data = await response.json();
      if (!Array.isArray(data.providers) || !Number.isFinite(data.server_time)
          || !Number.isFinite(data.fresh_seconds) || data.fresh_seconds <= 0) {
        throw new Error('Usage unavailable');
      }
      if (request !== controller) return;
      snapshot = {...data, providers: data.providers.filter(entry =>
        entry && Object.hasOwn(providers, entry.provider) && Array.isArray(entry.windows))};
      receivedAt = {wall: Date.now(), mono: performance.now()};
    } catch (_) {
      if (request !== controller) return;
      snapshot = null;
    } finally {
      clearTimeout(timeout);
      if (request === controller) {
        request = null;
        render();
      }
    }
  }

  function pause() {
    request?.abort();
    request = null;
  }

  strip.addEventListener('click', () => {
    render();
    sheet.showModal();
    strip.setAttribute('aria-expanded', 'true');
  });
  document.getElementById('usage-close').addEventListener('click', () => sheet.close());
  sheet.addEventListener('click', event => {
    if (event.target !== sheet) return;
    const box = sheet.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right
        || event.clientY < box.top || event.clientY > box.bottom) sheet.close();
  });
  sheet.addEventListener('close', () => {
    strip.setAttribute('aria-expanded', 'false');
    strip.focus({preventScroll: true});
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) pause();
    else {
      render();
      refresh();
    }
  });
  window.addEventListener('pagehide', pause);
  window.addEventListener('pageshow', event => {
    if (event.persisted) {
      render();
      refresh();
    }
  });
  setInterval(() => {
    if (document.hidden) return;
    render();
    if (performance.now() >= nextRead) refresh();
  }, 15_000);
  refresh();
})();
