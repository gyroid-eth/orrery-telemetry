// Pure fixed upstream path/preferences/route modules; no TUI or model request.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
const runtime = process.env.ORRERY_DSH_TEST_RUNTIME;
const node = process.env.ORRERY_DSH_TEST_NODE;
test('patched native preferences isolate A/B and preserve the user default', {
  skip: !runtime || !node ? 'explicit pinned native runtime and Node required' : false,
}, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-tui-datadir-'));
  try {
    const python = process.env.ORRERY_DSH_TEST_PYTHON ?? 'python3';
    const extract = spawnSync(python, ['-c', `import base64,hashlib,json,sys,tarfile
from pathlib import Path
runtime,repo,out=map(Path,sys.argv[1:])
pin=next(p for p in json.loads((repo/'integrations/dsh/runtime-pins.json').read_text()) if p['name'].endswith('/dsh-tui'))
tar=runtime.parent/'evidence'/pin['file']
assert 'sha512-'+base64.b64encode(hashlib.sha512(tar.read_bytes()).digest()).decode()==pin['integrity']
recipe=json.loads((repo/'integrations/dsh/tui-datadir.patch.json').read_text())
with tarfile.open(tar) as archive:
 for file in [recipe['file'],'lib/types/modelPrefs.js','lib/types/modelRoute.js']:
  text=archive.extractfile('package/'+file).read().decode()
  if file==recipe['file']:
   assert text.count(recipe['old'])==1
   text=text.replace(recipe['old'],recipe['new'],1)
  dest=out/file;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(text)
`, runtime, process.cwd(), root], {encoding: 'utf8'});
    assert.equal(extract.status, 0, extract.stderr);
    fs.writeFileSync(path.join(root, 'package.json'), '{"type":"module"}');
    const userDir = path.join(root, 'home/.dsh-tui');
    fs.mkdirSync(userDir, {recursive: true});
    const user = path.join(userDir, 'model.json');
    fs.writeFileSync(user, '{"provider":"user","model":"keep"}');
    const before = fs.readFileSync(user, 'utf8');
    const child = (dir, model, change) => {
      const result = spawnSync(node, ['--input-type=module', '-e', `
import {readModelPref,writeModelPref} from './lib/types/modelPrefs.js';
import {resolveModelRoute} from './lib/types/modelRoute.js';
if(${JSON.stringify(change)}!==null)writeModelPref('deepseek-official',${JSON.stringify(change)});
console.log(JSON.stringify(resolveModelRoute({provider:'deepseek-official',model:${JSON.stringify(model)}},readModelPref())));
`], {cwd: root, env: {HOME: path.join(root, 'home'), PATH: '/usr/bin:/bin', ...(dir ? {DSH_TUI_DATA_DIR: dir} : {})}, encoding: 'utf8'});
      assert.equal(result.status, 0, result.stderr);
      return JSON.parse(result.stdout);
    };
    const a = path.join(root, 'A'), b = path.join(root, 'B');
    for (const [dir, model] of [[a, 'model-a'], [b, 'model-b']]) {
      fs.mkdirSync(dir); fs.writeFileSync(path.join(dir, 'model.json'), JSON.stringify({provider: 'deepseek-official', model}));
    }
    assert.equal(child(a, 'deployment', null).model, 'model-a');
    assert.equal(child(b, 'deployment', null).model, 'model-b');
    assert.equal(child(a, 'deployment', 'changed-in-a').model, 'changed-in-a');
    assert.equal(child(b, 'deployment', null).model, 'model-b');
    assert.equal(child(null, 'deployment', null).model, 'keep');
    assert.equal(fs.readFileSync(user, 'utf8'), before);
  } finally {fs.rmSync(root, {recursive: true, force: true});}
});
