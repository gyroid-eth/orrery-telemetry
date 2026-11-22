// Exercise the pinned TUI's real preset registration seam without a model or service.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const runtime = process.env.ORRERY_DSH_TEST_RUNTIME;

test('generated preset patch reaches only the selected route in each native preset',
  {skip: !runtime && 'fixed runtime required'}, async () => {
  const base = path.join(runtime, 'node_modules/@deepseek-harness-tui/dsh-tui');
  const {registerBundledPresets} = await import(pathToFileURL(path.join(base, 'lib/types/dsh-adapter/bundled-presets.js')));
  const policy = {provider: 'deepseek-official', model: 'fixture', thresholdRatio: 0.8,
    headroomTokens: 13107, retainRatio: 0.16, maxTokens: 8192};
  const entries = ['standard', 'ptc', 'cordis'].map(preset => ({disabled: false, options: {
    name: '@deepseek-harness-tui/dsh-tui/preset-patch',
    config: {preset, config: {'compaction-basic': {modelPolicies: [policy]}}}}}));
  const registered = new Map();
  const service = {register: async definition => {registered.set(definition.id, definition); return () => {};}};
  const ctx = {baseUrl: pathToFileURL(path.join(runtime, 'package.json')).href,
    get: key => key === 'agentPresets' ? service : key === 'loader' ? {entries: () => entries} : undefined,
    logger: {warn: message => {throw Error(message);}}, effect: () => {}, extend: () => ctx};
  assert.equal(await registerBundledPresets(ctx), true);
  function descendants(rows) {return rows.flatMap(row => [row, ...(row.group === true && Array.isArray(row.config) ? descendants(row.config) : [])]);}
  for (const preset of ['standard', 'ptc', 'cordis']) {
    const rows = descendants(registered.get(preset).plugins);
    assert.deepEqual(rows.find(row => row.id === 'compaction-basic').config, {modelPolicies: [policy]});
    assert.ok(rows.some(row => row.id === 'tool-result-pruner'));
  }
  const minimal = descendants(registered.get('minimal').plugins).find(row => row.id === 'compaction-basic');
  assert.ok(minimal);
  assert.equal(minimal.config, undefined);
});

test('pinned native pressure policy honors scaled budgets and exact model routes',
  {skip: !runtime && 'fixed runtime required'}, async () => {
  const fs = await import('node:fs');
  const source = fs.readFileSync(path.join(runtime, 'node_modules/@deepseek-ai/dsh-compaction-basic/lib/index.js'), 'utf8');
  const start = source.indexOf('const DEFAULT_THRESHOLD_RATIO');
  const end = source.indexOf('//#endregion', start);
  assert.ok(start >= 0 && end > start);
  // Evaluate only the pinned package's pure policy definitions: no CLI, agent or network.
  const api = Function('deepFreeze', source.slice(start, end) + '\nreturn {resolveConfig, resolveTargetPolicy, resolveCompactSpec};')(value => value);
  for (const context of [8192, 16384, 32768, 64000, 131072, 262144, 1000000]) {
    const output = Math.min(8192, Math.floor(context / 4));
    const headroom = Math.floor(context / 10);
    const modelPolicy = {provider:'deepseek-official', model:'selected', thresholdRatio:0.8,
      headroomTokens:headroom, retainRatio:0.16, maxTokens:Math.min(output, headroom)};
    const config = api.resolveConfig({modelPolicies:[modelPolicy]});
    const selected = api.resolveTargetPolicy(config, {provider:'deepseek-official', model:'selected'});
    const spec = api.resolveCompactSpec(selected, context, output);
    assert.equal(spec.thresholdTokens, Math.floor(Math.min(context * 0.8, context - output - headroom)));
    assert.equal(spec.retainTokens, Math.floor((context - output) * 0.16));
    assert.equal(spec.maxTokens, Math.min(output, headroom));
    assert.ok(spec.retainTokens < spec.thresholdTokens);
    const other = api.resolveTargetPolicy(config, {provider:'deepseek-official', model:'other'});
    assert.equal(other.headroomTokens, 65536);
  }
});
