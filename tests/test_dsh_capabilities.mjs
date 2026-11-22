import assert from 'node:assert/strict';
import {test} from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {extractEfforts} from '../integrations/dsh/capabilities.mjs';
const runtime = process.env.ORRERY_DSH_TEST_RUNTIME;
const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

test('fixed dsh resolves both models without trial defaults or provider calls', {skip: !runtime && 'fixed runtime required'}, async () => {
  const input = ['deepseek','strata'].map(id => ({backend_id: id, model: id + '-custom-model',
    model_definition: {contextWindow: 131072}, model_fingerprint: id, reasoningEffort: 'low'}));
  const result = await extractEfforts(runtime, input);
  assert.equal(result.forwarded, 0); assert.equal(result.network_attempts, 0);
  for (const row of result.entries) {
    assert.deepEqual(row.efforts, ['off','low','high','max']);
    assert.equal(row.default_effort, 'high'); assert.equal(row.source, 'harness');
  }
  const disabled = await extractEfforts(runtime, [{backend_id:'deepseek', model:'configured', thinking:'disabled'}]);
  assert.deepEqual(disabled.entries[0].efforts, ['off']);
  assert.equal(disabled.entries[0].default_effort, 'off');
});

test('standalone extractor runs from root with only private HOME and PATH', {skip: !runtime && 'fixed runtime required'}, () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'capability-home-'));
  try {
    const result = spawnSync(process.execPath, [path.join(project,'integrations/dsh/capabilities.mjs')],
      {cwd:'/',env:{HOME:home,PATH:'/usr/bin:/bin'}, encoding:'utf8', timeout:10000,
       input:JSON.stringify({runtime, entries:[{backend_id:'strata',model:'configured-custom',model_fingerprint:'fixture'}]})});
    assert.equal(result.status,0,result.stderr);
    assert.equal(JSON.parse(result.stdout).network_attempts,0);
    assert.equal(JSON.parse(result.stdout).entries[0].default_effort,'high');
  } finally { fs.rmSync(home,{recursive:true,force:true}); }
});

test('a provider hook that swallows a blocked fetch cannot produce known capabilities', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'capability-negative-'));
  const previous = globalThis.fetch;
  function pkg(name, source) {
    const folder = path.join(root,'node_modules',name); fs.mkdirSync(folder,{recursive:true});
    fs.writeFileSync(path.join(folder,'package.json'),JSON.stringify({name,type:'module',main:'index.js'}));
    fs.writeFileSync(path.join(folder,'index.js'),source);
  }
  try {
    pkg('@deepseek-ai/cordis','export class Context {fiber={dispose(){}}}');
    pkg('@deepseek-ai/dsh-llm',`export class LlmRuntime {
      constructor(ctx){ctx.llm=this} registerAdapter(){return ()=>{}}
      async resolveModelInfo(){await fetch('https://not-allowed.invalid').catch(()=>{});
        return {reasoning:{efforts:[{id:'low'}],defaultEffort:'low'}}}
    }`);
    pkg('@deepseek-ai/dsh-llm-deepseek','export class DeepSeekAdapter {}\nexport const resolveAdapterOptions=x=>x');
    await assert.rejects(extractEfforts(root,[{backend_id:'strata',model:'fixture'}]),/NETWORK_FORBIDDEN/);
    assert.equal(globalThis.fetch,previous);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});
