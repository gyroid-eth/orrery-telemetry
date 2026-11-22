(() => {
  'use strict';
  if (location.pathname === '/reader-pane') {
    document.documentElement.dataset.readerPane = '';
    if (window.parent !== window) {
      try { window.OrreryMobilePane = window.parent.OrreryMobileWorkspace?.viewFor(window.frameElement); }
      catch (_) { /* Only an admitted workspace can bind the Reader. */ }
    }
    if (window.OrreryMobilePane) document.addEventListener('orrery-mobile-input-ready', () => initComposer(), {once:true});
    return;
  }

  const $ = selector => document.querySelector(selector);
  const svgNS = 'http://www.w3.org/2000/svg';
  const main = $('#mobile-main'), graph = $('#graph-panel'), graphSvg = $('#gsvg');
  const theme = $('#mobile-theme'), sidebar = $('.mobile-sidebar');
  const landscape = matchMedia('(orientation: landscape)');
  const cards = new Map(), weightByLayout = new Map();
  const graphRatios = {x: .62, y: .49};
  const bindings = new WeakMap(), identities = new Map();
  const graphView = () => window.OrreryMobileGraph;
  let opened = [], draft = new Set(), layout = 'graph', active = false, picking = false;
  let returnLayout = null, graphOnly = false, readerReady = false, readerSelection = null;

  function node(tag, cls, text) {
    const result = document.createElement(tag);
    if (cls) result.className = cls;
    if (text) result.textContent = text;
    return result;
  }
  function button(label, symbol) {
    const result = node('button', 'icon-button');
    result.type = 'button';
    result.title = label;
    result.setAttribute('aria-label', label);
    result.innerHTML = '<svg class="mobile-icon" aria-hidden="true"><use href="#icon-' + symbol + '"></use></svg>';
    return result;
  }
  const tools = node('div', 'workspace-tools');
  const open = button('Open agent workspace', 'agent-panes');
  const workspaceControls = node('div', 'workspace-toolbar');
  const layoutControls = node('nav', 'icon-group');
  layoutControls.setAttribute('aria-label', 'Workspace layout');
  const alongside = button('Graph and agent panes', 'graph');
  const full = button('Agent panes only', 'agent-panes');
  const add = button('Add agents', 'plus');
  const back = button('Back to graph', 'close');
  layoutControls.append(alongside, full);
  workspaceControls.append(layoutControls, add, back);
  const graphTools = node('div', 'workspace-toolbar');
  const graphCommit = node('button', 'workspace-commit', 'Open agents');
  graphCommit.type = 'button';
  const graphCancel = button('Cancel graph selection', 'close');
  const graphSearch = button('Search the agent list', 'search');
  graphTools.append(graphSearch, graphCommit, graphCancel);
  theme.before(tools);
  tools.append(open, workspaceControls, graphTools, theme);

  const pickIcon = document.createElementNS(svgNS, 'symbol');
  pickIcon.id = 'icon-terminal-pick';
  pickIcon.setAttribute('viewBox', '0 0 24 24');
  pickIcon.innerHTML = '<path d="M9 4H5a1 1 0 0 0-1 1v4m11-5h4a1 1 0 0 1 1 1v4M4 15v4a1 1 0 0 0 1 1h4m11-5v4a1 1 0 0 1-1 1h-4m-7-8 2.5 2.5 5-5"></path>';
  $('.icon-library').append(pickIcon);
  const panesIcon = document.createElementNS(svgNS, 'symbol');
  panesIcon.id = 'icon-agent-panes';
  panesIcon.setAttribute('viewBox', '0 0 24 24');
  panesIcon.innerHTML = '<rect x="3" y="5" width="18" height="14" rx="2"></rect><path d="M12 5v14"></path>';
  $('.icon-library').append(panesIcon);
  const chooseFromGraph = button('Select agents on the graph', 'terminal-pick');
  $('.graph-toolbar-actions').prepend(chooseFromGraph);
  const picker = node('dialog', 'workspace-picker');
  picker.setAttribute('aria-label', 'Open agents');
  const pickerHead = node('div', 'workspace-picker-head');
  const pickerGraph = button('Select from the graph', 'graph');
  const dismissPicker = button('Close agent picker', 'close');
  dismissPicker.autofocus = true;
  pickerHead.append(node('h2', '', 'Open agents'), pickerGraph, dismissPicker);
  const search = node('input', 'workspace-picker-search');
  search.type = 'search';
  search.placeholder = 'Find an agent';
  search.setAttribute('aria-label', 'Find an agent');
  const roster = node('div', 'workspace-roster');
  const pickerActions = node('div', 'workspace-picker-actions');
  const apply = node('button', 'workspace-commit', 'Open agents');
  apply.type = 'button';
  pickerActions.append(apply);
  picker.append(pickerHead, search, roster, pickerActions);
  document.body.append(picker);

  const panes = node('section', 'workspace-panes');
  panes.setAttribute('aria-label', 'Agent workspace');
  main.append(panes);

  function setCount() {
    for (const control of [apply, graphCommit]) {
      control.textContent = draft.size ? (opened.length ? 'Show ' : 'Open ') + draft.size : 'Open agents';
      control.disabled = draft.size === 0;
    }
  }
  function populatePicker() {
    const term = search.value.trim().toLocaleLowerCase();
    roster.replaceChildren();
    for (const agent of graphView().agents().filter(a => (a.name + ' ' + (a.role || '')).toLocaleLowerCase().includes(term))) {
      const row = node('label', 'workspace-agent-choice');
      const mark = node('input');
      mark.type = 'checkbox';
      mark.checked = draft.has(agent.name);
      mark.dataset.agent = agent.name;
      mark.onchange = () => {
        if (mark.checked) draft.add(agent.name);
        else draft.delete(agent.name);
        setCount();
      };
      row.append(OrreryMobileProfile.face(agent.name), node('span', 'name', agent.name), mark);
      roster.append(row);
    }
    if (!roster.children.length) roster.append(node('p', 'empty', 'No matching agents'));
    setCount();
  }
  function openPicker(selection = opened, origin = null) {
    if (!readerReady) return;
    readerSelection = origin;
    draft = new Set(selection);
    search.value = '';
    populatePicker();
    if (!picker.open) picker.showModal();
  }
  function paintGraphChecks() {
    for (const item of graphSvg.querySelectorAll('.node[data-name]')) {
      const selected = picking && draft.has(item.dataset.name);
      item.classList.toggle('workspace-picked', selected);
      if (picking) item.setAttribute('aria-pressed', String(selected));
      else item.removeAttribute('aria-pressed');
      const old = item.querySelector('.workspace-check');
      if (selected && !old) {
        const mark = document.createElementNS(svgNS, 'g');
        mark.setAttribute('class', 'workspace-check');
        mark.setAttribute('aria-hidden', 'true');
        mark.setAttribute('pointer-events', 'none');
        mark.innerHTML = '<circle class="workspace-check-disc" cx="19" cy="-20" r="8"></circle><path class="workspace-check-mark" d="M15.5 -20l2.5 2.5 4.5-5"></path>';
        item.append(mark);
      } else if (!selected && old) old.remove();
    }
  }
  function toggleAgent(name) {
    if (draft.has(name)) draft.delete(name);
    else draft.add(name);
    paintGraphChecks();
    setCount();
  }
  new MutationObserver(paintGraphChecks).observe(graphSvg, {childList:true});

  function startGraphPick(selection = opened) {
    if (!readerReady) return;
    draft = new Set(selection);
    if (picker.open) picker.close();
    if (readerSelection) window.OrreryMobileTerminal.close();
    returnLayout = layout;
    if (active && layout === 'full') layout = 'graph';
    graphView().show();
    picking = true;
    graphView().pickWith(toggleAgent);
    render();
    graphCommit.focus({preventScroll:true});
  }
  function stopGraphPick(restore) {
    picking = false;
    graphView().pickWith(null);
    if (restore && returnLayout) layout = returnLayout;
    returnLayout = null;
    paintGraphChecks();
  }
  function openSelection() {
    if (!draft.size) return;
    if (opened.some(name => !draft.has(name) && !canRemove(name))) {
      (picking ? graphCommit : apply).title = 'Check the pending input before closing its pane';
      return;
    }
    for (const name of opened) if (!draft.has(name)) removeCard(name);
    for (const name of draft) if (!identities.has(name)) {
      identities.set(name, readerSelection?.name === name ? readerSelection.identity : window.OrreryMobileIdentity.forName(name));
    }
    opened = [...draft];
    if (picker.open) picker.close();
    if (readerSelection && $('#terminal-sheet').open) window.OrreryMobileTerminal.close();
    readerSelection = null;
    stopGraphPick(true);
    graphView().show();
    active = true;
    render();
    add.focus({preventScroll:true});
  }
  function paintHeader() {
    open.hidden = !readerReady || active || picking;
    workspaceControls.hidden = !active || picking;
    graphTools.hidden = !picking;
    chooseFromGraph.hidden = !readerReady || picking;
    for (const [control, selected] of [[alongside,layout === 'graph'], [full,layout === 'full']]) {
      control.classList.toggle('active', selected);
      control.setAttribute('aria-pressed', String(selected));
    }
  }

  function canRemove(name) {
    return cards.get(name)?.firstElementChild?.contentWindow?.OrreryMobileTerminal?.canLeave() !== false;
  }

  function removeCard(name) {
    const card = cards.get(name), frame = card?.firstElementChild;
    if (frame) {
      leaveComposer(frame);
      frame.contentWindow?.OrreryMobileTerminal?.close(true,true);
    }
    card?.remove();
    cards.delete(name);
    identities.delete(name);
    for (const weights of weightByLayout.values()) weights.delete(name);
  }

  function cardFor(name) {
    if (cards.has(name)) return cards.get(name);
    const card = node('section', 'workspace-card');
    card.dataset.name = name;
    card.setAttribute('aria-label', name);
    const frame = node('iframe', 'workspace-reader-frame');
    frame.title = 'Reading View for ' + name;
    frame.src = '/reader-pane';
    frame.allow = 'clipboard-write';
    bindings.set(frame, {
      name, identity:identities.get(name), parent:window,
      compact: () => opened.length > 1,
      expand: () => activateComposer(frame),
      fold: () => leaveComposer(frame),
      close: () => closePane(frame),
      openPeer: (agent, identity) => {
        if (!opened.includes(agent)) {
          identities.set(agent, identity);
          opened.push(agent);
          render();
        }
        cardFor(agent).firstElementChild.focus({preventScroll:true});
      },
      unavailable: () => {
        const note = node('p', 'workspace-unavailable', 'Reader unavailable');
        note.setAttribute('role', 'status');
        card.append(note);
      },
    });
    card.append(frame);
    cards.set(name, card);
    panes.append(card);
    return card;
  }

  const composeBackdrop = node('div', 'workspace-compose-backdrop');
  composeBackdrop.hidden = true;
  document.body.append(composeBackdrop);
  let composeFrame, inertWorkspace = [];

  function resizeWorkspaceViewport() {
    const viewport = window.visualViewport;
    const height = viewport?.height ?? innerHeight;
    document.body.style.setProperty('--workspace-visible-height', height + 'px');
    document.body.style.setProperty('--workspace-compose-bottom',
      Math.max(0, innerHeight - height - (viewport?.offsetTop ?? 0)) + 'px');
  }

  function leaveComposer(frame) {
    if (!frame || composeFrame !== frame) return;
    delete frame.dataset.composerOverlay;
    frame.style.removeProperty('--workspace-compose-height');
    composeFrame = null;
    composeBackdrop.hidden = true;
    for (const item of inertWorkspace) item.inert = false;
    inertWorkspace = [];
  }

  composeBackdrop.onclick = () => composeFrame?.contentWindow?.OrreryMobileComposer?.fold(true);

  function activateComposer(frame) {
    if (frame === composeFrame) return true;
    if (composeFrame) {
      composeFrame.contentWindow?.OrreryMobileComposer?.fold();
      if (composeFrame) return false;
    }
    composeFrame = frame;
    frame.dataset.composerOverlay = 'true';
    composeBackdrop.hidden = false;
    const card = frame.closest('.workspace-card');
    inertWorkspace = [$('body>header'), graph, sidebar, workspaceDivider,
      ...Array.from(panes.children).filter(item => item !== card)].filter(item => item && !item.inert);
    for (const item of inertWorkspace) item.inert = true;
    resizeWorkspaceViewport();
    return true;
  }

  function closePane(frame) {
    const card = frame.closest('.workspace-card');
    if (cards.get(card?.dataset.name) !== card || !canRemove(card.dataset.name)) return;
    const name = card.dataset.name;
    removeCard(name);
    opened = opened.filter(item => item !== name);
    active = opened.length > 0;
    render();
    (active ? add : open).focus({preventScroll:true});
  }

  const length = (element, axis) => element.getBoundingClientRect()[axis === 'x' ? 'width' : 'height'];
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

  function divider(title, axis, measure, applySize, finishSize) {
    const handle = node('div', 'workspace-divider');
    handle.tabIndex = 0;
    handle.dataset.axis = axis;
    handle.setAttribute('role', 'separator');
    handle.setAttribute('aria-orientation', axis === 'x' ? 'vertical' : 'horizontal');
    handle.setAttribute('aria-label', title);
    let gesture;
    const point = event => axis === 'x' ? event.clientX : event.clientY;
    function apply(value, state) {
      const {min, max, total} = state;
      if (min > max || total <= 0) return;
      const next = clamp(value, min, max);
      applySize(next, state);
      handle.setAttribute('aria-valuenow', Math.round(100 * next / total));
      return next;
    }
    handle.onpointerdown = event => {
      if (event.button !== 0 || gesture) return;
      handle.setPointerCapture(event.pointerId);
      gesture = {id:event.pointerId, start:point(event), state:measure()};
      handle.classList.add('workspace-dragging');
    };
    handle.onpointermove = event => {
      if (gesture?.id !== event.pointerId) return;
      gesture.next = apply(gesture.state.current + point(event) - gesture.start, gesture.state);
    };
    function finish(event, cancelled) {
      if (gesture?.id !== event.pointerId) return;
      const {state, next} = gesture;
      gesture = null;
      handle.classList.remove('workspace-dragging');
      if (next !== undefined) finishSize?.(cancelled ? null : next, state);
    }
    handle.onpointerup = event => finish(event, false);
    handle.onpointercancel = event => finish(event, true);
    handle.onlostpointercapture = event => finish(event, true);
    handle.onkeydown = event => {
      const steps = axis === 'x' ? {ArrowLeft:-24, ArrowRight:24} : {ArrowUp:-24, ArrowDown:24};
      const state = measure();
      let next;
      if (steps[event.key] !== undefined) next = apply(state.current + steps[event.key], state);
      else if (event.key === 'Home') next = apply(state.min, state);
      else if (event.key === 'End') next = apply(state.max, state);
      else return;
      event.preventDefault();
      if (next !== undefined) finishSize?.(next, state);
    };
    requestAnimationFrame(() => {
      if (!handle.isConnected) return;
      const state = measure();
      if (state.total > 0) {
        handle.setAttribute('aria-valuemin', Math.round(100 * state.min / state.total));
        handle.setAttribute('aria-valuemax', Math.round(100 * Math.max(state.min,state.max) / state.total));
        handle.setAttribute('aria-valuenow', Math.round(100 * state.current / state.total));
      }
    });
    return handle;
  }

  const homeDivider = divider('Resize graph and attention', 'y', () => {
    const current = length(graph,'y'), total = current + length(sidebar,'y');
    return {current,total,min:Math.min(225,total*.65),max:total-Math.min(46,total*.25)};
  }, (pixels, {total}) => {
    const graphRatio = pixels / total;
    main.style.setProperty('--workspace-home-graph', graphRatio + 'fr');
    main.style.setProperty('--workspace-home-attention', 1 - graphRatio + 'fr');
    main.dataset.homeResized = 'true';
    homeDivider.setAttribute('aria-valuetext', Math.round(100*(1-graphRatio)) + '% Attention');
  });
  homeDivider.classList.add('workspace-home-divider');
  graph.after(homeDivider);

  const outerAxis = () => landscape.matches ? 'x' : 'y';
  const paneAxis = () => layout === 'graph' ? (landscape.matches ? 'y' : 'x') : outerAxis();
  let workspaceDivider;
  function paintOuter(ratio) {
    main.style.setProperty('--workspace-graph', ratio + 'fr');
    main.style.setProperty('--workspace-pane', 1 - ratio + 'fr');
  }
  function makeOuterDivider(axis) {
    function endpoint(pixels, total) {
      const edge = Math.min(56, total / 8);
      if (pixels <= edge) return 'reader';
      if (total - pixels <= edge) return 'graph';
      return '';
    }
    const handle = divider('Resize graph and Reading View', axis, () => {
      const current = length(graph, axis), total = current + length(panes, axis);
      return {current, total, min:0, max:total};
    }, (pixels, {total}) => {
      paintOuter(pixels / total);
      const end = endpoint(pixels, total);
      handle.classList.toggle('workspace-at-edge', !!end);
      handle.setAttribute('aria-valuetext', end === 'reader' ? 'Reading View only'
        : end === 'graph' ? 'Graph only' : Math.round(100 * pixels / total) + '% Graph');
    }, (pixels, {total}) => {
      handle.classList.remove('workspace-at-edge');
      if (pixels === null) {
        paintOuter(graphRatios[axis]);
      } else {
        const end = endpoint(pixels, total);
        if (end) {
          if (picking) stopGraphPick(false);
          layout = end === 'reader' ? 'full' : 'graph';
          active = end === 'reader';
          graphOnly = !active;
          render();
          (active ? alongside : open).focus({preventScroll:true});
          return;
        }
        const graphMin = Math.min(axis === 'x' ? 340 : 215, total / 2);
        const readerMin = Math.min(axis === 'x' ? 270 : 215, total / 2);
        graphRatios[axis] = clamp(pixels, graphMin, total - readerMin) / total;
        paintOuter(graphRatios[axis]);
      }
      const percent = Math.round(100 * graphRatios[axis]);
      handle.setAttribute('aria-valuenow', percent);
      handle.setAttribute('aria-valuetext', percent + '% Graph');
    });
    return handle;
  }
  function fillPanes(axis) {
    const setKey = layout + '/' + axis;
    if (!weightByLayout.has(setKey)) weightByLayout.set(setKey, new Map());
    const weights = weightByLayout.get(setKey);
    function paintWeights() {
      for (const name of opened) cardFor(name).style.flexGrow = weights.get(name) ?? 1;
    }
    panes.dataset.axis = axis;
    for (const handle of panes.querySelectorAll(':scope > .workspace-divider')) handle.remove();
    for (const [index, name] of opened.entries()) {
      const current = cardFor(name);
      current.firstElementChild.contentWindow?.OrreryMobileComposer?.setCompact(opened.length > 1);
      current.style.order = 2 * index;
      if (index > 0) {
        const before = cardFor(opened[index-1]);
        const minimum = axis === 'x' ? 212 : 164;
        const handle = divider('Resize ' + before.dataset.name + ' and ' + name, axis, () => {
          const size = length(before, axis), total = size + length(current, axis);
          return {current:size, total, min:minimum, max:total-minimum};
        }, (pixels, state) => {
          const totalWeight = (weights.get(before.dataset.name) ?? 1) + (weights.get(name) ?? 1);
          weights.set(before.dataset.name, totalWeight * pixels / state.total);
          weights.set(name, totalWeight * (1-pixels/state.total));
          paintWeights();
        });
        handle.style.order = 2 * index - 1;
        panes.append(handle);
      }
    }
    paintWeights();
  }
  function render() {
    if (active) graphOnly = false;
    main.classList.toggle('workspace-graph-only', graphOnly);
    main.classList.toggle('workspace-active', active);
    main.classList.toggle('workspace-picking', picking);
    main.dataset.terminalLayout = layout;
    const axis = outerAxis();
    main.dataset.splitAxis = axis;
    paintHeader();
    paintGraphChecks();
    setCount();
    workspaceDivider?.remove();
    panes.hidden = !active;
    if (!active) return;
    if (layout === 'graph') {
      paintOuter(graphRatios[axis]);
      workspaceDivider = makeOuterDivider(axis);
      graph.after(workspaceDivider);
    }
    fillPanes(paneAxis());
  }

  open.onclick = () => {
    if (!opened.length) {openPicker();return;}
    graphView().show();
    active = true;
    render();
    add.focus({preventScroll:true});
  };
  add.onclick = () => openPicker();
  back.onclick = () => {active = false;render();open.focus({preventScroll:true});};
  alongside.onclick = () => {layout = 'graph';render();};
  full.onclick = () => {layout = 'full';render();};
  dismissPicker.onclick = () => {readerSelection=null;picker.close();};
  picker.addEventListener('cancel', () => {readerSelection=null;});
  apply.onclick = graphCommit.onclick = openSelection;
  chooseFromGraph.onclick = () => {readerSelection=null;startGraphPick();};
  pickerGraph.onclick = () => startGraphPick(draft);
  graphSearch.onclick = () => {stopGraphPick(true);render();openPicker(draft,readerSelection);};
  graphCancel.onclick = () => {readerSelection=null;stopGraphPick(true);render();(active ? add : open).focus({preventScroll:true});};
  search.oninput = populatePicker;
  landscape.addEventListener('change', render);
  window.visualViewport?.addEventListener('resize', resizeWorkspaceViewport);
  window.visualViewport?.addEventListener('scroll', resizeWorkspaceViewport);
  window.addEventListener('resize', () => {
    resizeWorkspaceViewport();
    if (main.dataset.splitAxis !== outerAxis()) render();
  });
  window.OrreryMobileWorkspace = Object.freeze({
    viewFor(frame){return bindings.get(frame);},
    refresh(){
      if (picker.open) {
        const focused = document.activeElement?.dataset.agent;
        populatePicker();
        if (focused) [...roster.querySelectorAll('input')].find(input=>input.dataset.agent===focused)?.focus({preventScroll:true});
      }
      for (const card of cards.values()) {
        const conversation = card.firstElementChild.contentWindow?.OrreryMobileConversation;
        conversation?.refreshProfiles();
        conversation?.refreshActivity();
      }
    },
    readerEntry(name,identity){
      const pin = button('Add ' + name + ' to the workspace', 'plus');
      pin.classList.add('reader-icon');
      pin.onclick = () => {
        if (!window.OrreryMobileTerminal.canLeave()) return;
        openPicker([...opened,name],{name,identity});
      };
      return pin;
    },
  });
  document.addEventListener('orrery-mobile-input-ready', () => {
    readerReady = window.OrreryMobileTerminal.available();
    render();
  });
  new ResizeObserver(() => graphView()?.resize()).observe(graphSvg);
  resizeWorkspaceViewport();
  render();

  function initComposer() {
    const workspace = window.OrreryMobilePane, agent = workspace.name;
    const sheet = document.getElementById('terminal-sheet'), root = document.documentElement;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const command = document.createElement('button');
    const collapse = document.createElement('button');
    command.type = collapse.type = 'button';
    command.className = 'reader-icon workspace-compose-button';
    collapse.className = 'reader-icon workspace-collapse-button';
    command.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15.5 5.5 3 3M4 20l4.5-1 11-11a2.1 2.1 0 0 0-3-3l-11 11L4 20z"/></svg><span aria-hidden="true"></span>';
    collapse.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>';
    collapse.title = 'Close input';
    collapse.setAttribute('aria-label', 'Close input for ' + agent);
    command.setAttribute('aria-controls', 'workspace-compose-panel');

    let compact = workspace.compact(), expanded = false;
    let pressMotion, openMotion, fadeMotion, closing, observedPanel, observedBar;
    const frame = window.frameElement;
    const overlay = () => frame.dataset.composerOverlay === 'true';
    const measure = () => {
      if (overlay() && observedPanel && observedBar) {
        const height = observedPanel.offsetHeight + observedBar.offsetHeight;
        if (height) frame.style.setProperty('--workspace-compose-height', height + 'px');
      }
    };
    const metrics = new ResizeObserver(measure);
    const panel = () => sheet.querySelector('.reader-input-bar');
    const field = () => sheet.querySelector('#terminal-text');
    const hasDraft = () => Boolean(field()?.value.trim() || panel()?.classList.contains('has-reference') || panel()?.querySelector('.reader-images'));
    const locked = () => (!window.OrreryMobileTerminal.canLeave() || !!panel()?.matches('.reader-review'));

    // Command button physics, adapted to expand the existing Reader composer.
    function springFrames(duration, stiffness, damping, mass, start, end, shape) {
      const decay = damping / (2 * mass);
      const frequency = Math.sqrt(stiffness / mass - decay * decay);
      return Array.from({length:31}, (_,index) => {
        const t = index * duration / 30000;
        const displacement = Math.exp(-decay*t) * (Math.cos(frequency*t) + decay/frequency*Math.sin(frequency*t));
        return shape(index === 30 ? end : end + (start-end)*displacement);
      });
    }

    function press(down) {
      const current = command.getBoundingClientRect().width / (command.offsetWidth || 44);
      pressMotion?.cancel();
      if (command.hidden || reduced.matches || !command.animate) return;
      pressMotion = command.animate(springFrames(280,520,34,.6,current,down ? .97 : 1,
        scale => ({transform:`scale(${scale})`})), {duration:280,fill:'forwards'});
    }
    command.addEventListener('pointerdown', () => press(true));
    for (const event of ['pointerup','pointerleave','pointercancel']) command.addEventListener(event, () => press(false));

    function sync() {
      const input = panel();
      root.toggleAttribute('data-workspace-compose-overlay', overlay());
      if (!input) {root.dataset.workspaceComposer='standard';command.hidden=true;collapse.hidden=true;return;}
      input.id = 'workspace-compose-panel';
      const open = expanded || locked();
      const draft = hasDraft();
      const folding = closing === input;
      input.inert = folding;
      root.dataset.workspaceComposer = compact ? (open || folding ? 'expanded' : 'collapsed') : 'standard';
      command.hidden = !compact || open || folding;
      collapse.hidden = !compact || !open || locked();
      command.dataset.draft = String(draft);
      command.title = draft ? 'Open draft' : 'Write a message';
      command.setAttribute('aria-label', command.title + ' for ' + agent);
      command.setAttribute('aria-expanded', String(open));
      if (!command.isConnected) sheet.append(command);
      const bar = sheet.querySelector('.reader-bar');
      if (bar && collapse.parentNode !== bar) bar.append(collapse);
      if (observedPanel !== input || observedBar !== bar) {
        metrics.disconnect();
        observedPanel = input;
        observedBar = bar;
        if (bar) metrics.observe(bar);
        metrics.observe(input);
      }
      measure();
    }

    function morph(opening, commandBounds) {
      openMotion?.cancel();
      fadeMotion?.cancel();
      const rect = frame.getBoundingClientRect();
      if (reduced.matches || !frame.animate || !rect.width || !rect.height) return;
      const card = frame.closest('.workspace-card').getBoundingClientRect();
      const anchor = commandBounds ?? {left:card.right-57, top:card.bottom-57, width:44,height:44};
      const x = anchor.width / rect.width, y = anchor.height / rect.height;
      const dx = anchor.left - rect.left, dy = anchor.top - rect.top;
      openMotion = frame.animate(springFrames(500,300,28,1,opening ? 0 : 1,opening ? 1 : 0,t => ({
        transform:`translate(${dx*(1-t)}px,${dy*(1-t)}px) scale(${x+(1-x)*t},${y+(1-y)*t})`,
        transformOrigin:'top left',
      })), {duration:500});
      if (opening) {
        const animation = openMotion;
        setTimeout(() => {if (openMotion === animation) animation.cancel();}, 540);
      }
      const input = panel();
      fadeMotion = input.animate([{opacity:0,filter:'blur(4px)'},{opacity:1,filter:'blur(0)'}], {
        duration:180,delay:opening ? 60 : 0,easing:'cubic-bezier(.23,1,.32,1)',
        direction:opening ? 'normal' : 'reverse',fill:opening ? 'backwards' : 'forwards',
      });
      return openMotion;
    }

    function open(focus) {
      const input = panel();
      if (!compact || !input) return;
      const button = command.getBoundingClientRect(), card = frame.getBoundingClientRect();
      const origin = {left:button.left+card.left, top:button.top+card.top, width:button.width || 44,height:button.height || 44};
      if (!workspace.expand()) return;
      closing = null;
      const previous = expanded;
      expanded = true;
      sync();
      if (!previous) morph(true,origin);
      if (focus) {
        const text = field();
        (text?.disabled ? input.querySelector('.reader-input-target') : text)?.focus({preventScroll:true});
      }
    }

    function fold(restoreFocus = false) {
      if (!compact || !expanded || locked()) return;
      const input = panel();
      if (input?.contains(document.activeElement)) document.activeElement.blur();
      expanded = false;
      if (!restoreFocus || !input || reduced.matches) {
        openMotion?.cancel();
        fadeMotion?.cancel();
        workspace.fold();
        sync();
        if (restoreFocus) command.focus({preventScroll:true});
        return;
      }
      closing = input;
      sync();
      const animation = morph(false);
      function finish() {
        if (closing !== input) return;
        closing = null;
        animation?.cancel();
        fadeMotion?.cancel();
        workspace.fold();
        sync();
        if (!command.hidden) {
          command.focus({preventScroll:true});
        }
      }
      if (animation) {
        animation.finished.then(finish,finish);
        setTimeout(finish,540);
      }
      else finish();
    }

    command.onclick = () => open(true);
    collapse.onclick = () => fold(true);
    sheet.addEventListener('input', event => {if (event.target === field()) sync();});
    sheet.addEventListener('pointerdown', event => {
      if (expanded && !overlay() && !panel()?.contains(event.target) && !command.contains(event.target) && !hasDraft()) fold();
    });
    sheet.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || !expanded || !compact || locked() || sheet.querySelector('.reader-attachment-sources:not([hidden])')) return;
      event.preventDefault();
      fold(true);
    }, true);
    sheet.addEventListener('cancel', event => {
      if (event.target !== sheet || !overlay()) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      fold(true);
    }, true);
    const reference = OrreryMobileTerminal.reference;
    OrreryMobileTerminal.reference = value => {
      const accepted = reference(value);
      if (accepted && compact) open(true);
      return accepted;
    };
    window.OrreryMobileComposer = {
      setCompact(value) {
        if (compact === value) return;
        compact = value;
        expanded = false;
        closing = null;
        openMotion?.cancel();
        fadeMotion?.cancel();
        workspace.fold();
        sync();
      },
      fold,
    };
    new MutationObserver(sync).observe(sheet,{childList:true,subtree:true});
    sync();
  }

})();
