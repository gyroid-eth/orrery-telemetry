"""Exercise the pinned, real ToolRuntime pipeline with a filesystem fixture.

Set ORRERY_DSH_TEST_NODE and ORRERY_DSH_TEST_RUNTIME to the isolated pinned
installation. This does not mount a TUI, register an identity, or call a provider.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class ManagedRegistrySpike(unittest.TestCase):
    def test_patched_tui_controls(self):
        node = os.environ.get('ORRERY_DSH_TEST_NODE')
        runtime = os.environ.get('ORRERY_DSH_TEST_RUNTIME')
        if not node or not runtime:
            self.skipTest('explicit isolated Node/runtime required')
        script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
const [runtime,state,entry]=process.argv.slice(2);
const {verifyTuiPatch}=await import(pathToFileURL(entry));
const hash=f=>createHash('sha256').update(fs.readFileSync(f)).digest('hex');
const recipe=path.join(state,'recipe.json'), receipt=path.join(state,'receipt.json');
const a=path.join(state,'a.js'), b=path.join(state,'b.js');
fs.writeFileSync(a,'patched-a');fs.writeFileSync(b,'patched-b');
fs.writeFileSync(recipe,JSON.stringify({replacements:[{file:'a.js'},{file:'b.js'}]}));
fs.writeFileSync(receipt,JSON.stringify({recipe_sha256:hash(recipe),changes:[{file:'a.js',after:hash(a)},{file:'b.js',after:hash(b)}]}));
const verify=()=>verifyTuiPatch({tuiRoot:state,recipeFile:recipe,receiptFile:receipt});
assert.doesNotThrow(verify);
fs.writeFileSync(a,'unpatched-a');assert.throws(verify,/PATCH_FILE_MISMATCH/);
fs.writeFileSync(b,'unpatched-b');assert.throws(verify,/PATCH_FILE_MISMATCH/);
assert.throws(()=>verifyTuiPatch({tuiRoot:state,recipeFile:recipe,receiptFile:receipt+'.missing'}));
process.env.ORRERY_DSH_TUI_DATA_ROOT=state;
let fetches=0,streams=0;
globalThis.fetch=()=>{fetches++;throw new Error('OFFLINE_FIXTURE');};
const root=path.join(runtime,'node_modules/@deepseek-harness-tui/dsh-tui/lib/types');
const load=async file=>import(pathToFileURL(path.join(root,file)));
const {fetchBalance}=await load('deepseekBalance.js');
assert.equal((await fetchBalance('offline-dummy',{fetchImpl:globalThis.fetch})).reason,'unsupported');
const {checkForTuiUpdate}=await load('update.js');
assert.equal(await checkForTuiUpdate(),undefined);
const {runSideQuestion}=await load('dsh-adapter/sideQuestion.js');
assert.equal((await runSideQuestion({stream:()=>{streams++;throw new Error('NO_STREAM');}})).error,'unsupported');
const {createReportActions}=await load('dsh-adapter/channel/reports.js');
const actions=createReportActions({},{});
assert.equal(actions.initWorkspace(),'unsupported');assert.equal(actions.exportSession(),null);
const {IdeChannel}=await load('dsh-adapter/ide-channel.js');
assert.equal(await IdeChannel.prototype.start.call({}),undefined);
const {openInjectChannel}=await load('dsh-adapter/inject-channel.js');
assert.equal(typeof openInjectChannel('fixture',state,{},()=>{}).close,'function');
const {credentialStoreFiles}=await load('utils/credentials.js');
assert.deepEqual(credentialStoreFiles(),[]);
const {DATA_DIR}=await load('utils/paths.js'); assert.equal(DATA_DIR,state);
const {createTuiSettingsService}=await load('dsh-adapter/tui-settings.js');
// A deliberately nonexistent profile is not imported; startup needs no legacy settings.
assert.ok(createTuiSettingsService({profile:'offline-nonexistent',ns:{},keys:[]}));
assert.equal(fetches,0);assert.equal(streams,0);assert.equal(fs.existsSync(path.join(state,'AGENTS.md')),false);
console.log(JSON.stringify({patched_tui_controls:true,fetches,streams,credentials:0,init_export_disabled:true}));
'''
        with tempfile.TemporaryDirectory(prefix='dsh-controls-spike-') as root:
            entry = Path(root) / 'controls.mjs'
            entry.write_text(script)
            managed_entry = Path(__file__).resolve().parents[1] / 'integrations/dsh/managed_entry.mjs'
            result = subprocess.run([node, str(entry), runtime, root, str(managed_entry)], text=True,
                                    capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())

    def test_real_registry_guard(self):
        node = os.environ.get('ORRERY_DSH_TEST_NODE')
        runtime = os.environ.get('ORRERY_DSH_TEST_RUNTIME')
        if not node or not runtime:
            self.skipTest('explicit isolated Node/runtime required')
        plugin = Path(__file__).resolve().parents[1] / 'integrations/dsh/managed_plugin.mjs'
        script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const [runtime, plugin, fixture] = process.argv.slice(2);
const req = createRequire(path.join(runtime, 'package.json'));
const {Context} = await import(pathToFileURL(req.resolve('@deepseek-ai/cordis')));
const {SystemPrompt} = await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-system-prompt')));
const {ToolRuntime, defineTool} = await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-tools')));
const {createReservationGuard, mailReceipt, installSessionEvents} = await import(pathToFileURL(plugin));
const {SessionStore} = await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-session')));
const root = path.join(fixture, 'work'); fs.mkdirSync(root);
const outside = path.join(fixture, 'outside.txt'); fs.writeFileSync(outside, 'private');
fs.symlinkSync(outside, path.join(root, 'escape.txt'));
const events = [], attempt = {phase:'spike', agent_name:'FixtureAgent'};
let clock = Date.now();
const task = {worktree:root, allowed_paths:['allowed/**'], reservation_receipts:[{
  id:1, owner:'FixtureAgent', path_pattern:'allowed/**', expires_ts:new Date(clock+60000).toISOString(),
}]};
const policy = createReservationGuard({attempt,task,emit:e=>events.push(e),now:()=>clock});
const ctx = new Context();
await ctx.plugin(SystemPrompt, {}).await();
await ctx.plugin(ToolRuntime, {mode:'native'}).await();
await ctx.plugin(SessionStore).await();
let writes=0;
for (const name of ['read','write','bash']) ctx.tools.register(defineTool({
 name, description:'Offline fixture body', parameters:{
  file_path:{type:'string',required:true}, content:{type:'string'},
 }, output:{schema:{type:'string'},render:(_,v)=>[{type:'text',text:v}]},
 async execute(args){
  if(name==='write') {writes++;fs.mkdirSync(path.dirname(path.resolve(root,args.file_path)),{recursive:true});fs.writeFileSync(path.resolve(root,args.file_path),args.content);return 'written';}
  return name==='read'?fs.readFileSync(path.resolve(root,args.file_path),'utf8'):'not executed';
 },
}));
ctx.tools.guard(policy.guard);
// An extensible allow listener cannot undo the monotonic guard.
ctx.on('tools/pre-execute', async () => ({kind:'allow'}));
let id=0;
const call=(name,file_path,content='hello')=>ctx.tools.execute({callId:'fixture-'+(++id),name,
 arguments:{file_path:path.resolve(root,file_path),content},signal:new AbortController().signal});
try {
 assert.equal((await call('write','allowed/ok.txt')).isError,false);
 assert.equal(writes,1); assert.equal(fs.readFileSync(path.join(root,'allowed/ok.txt'),'utf8'),'hello');
 assert.equal((await call('read','allowed/ok.txt')).isError,false);
 for(const target of ['../outside.txt','escape.txt','not-reserved.txt','.git/config'])
  assert.equal((await call('write',target,'bad')).isError,true,target);
 assert.equal(writes,1);assert.equal(fs.readFileSync(outside,'utf8'),'private');
 clock += 120000;
 assert.equal((await call('write','allowed/expired.txt')).isError,true);
 assert.equal(events.at(-1).reason,'reservation-expired'); assert.equal(writes,1);
 assert.equal((await call('bash','allowed/anything')).isError,true);
 assert.equal(events.at(-1).reason,'not-in-wire-tools');
 const absent=createReservationGuard({attempt,task:{...task,reservation_receipts:[]},emit:()=>{}});
 assert.equal(absent.guard({name:'write',arguments:{file_path:path.join(root,'allowed/no.txt')}}),'not-reserved');
 const wrong=createReservationGuard({attempt:{...attempt,agent_name:'OtherAgent'},task,emit:()=>{}});
 assert.equal(wrong.guard({name:'write',arguments:{file_path:path.join(root,'allowed/no.txt')}}),'not-reserved');
 assert.equal(policy.guard({name:'write',arguments:{file_path:'allowed/relative.txt'}}),'outside-worktree');
 assert.equal(mailReceipt({structuredContent:{verified_sender:false,deliveries:[{payload:{id:9,to:['Parent'],from:'FixtureAgent'}}]}}).ok,false);
 assert.deepEqual(mailReceipt({structuredContent:{verified_sender:true,deliveries:[{payload:{id:9,to:['Parent'],from:'FixtureAgent'}}]}}),
  {message_id:9,to:'Parent',from:'FixtureAgent',verified_sender:true,ok:true});
 const current = ctx.sessions.create(), other = ctx.sessions.create();
 const turns=[];
 installSessionEvents(ctx,e=>turns.push(e),()=>current.id);
 other.append('turn/start',{turn:1});
 current.append('turn/start',{turn:1});
 current.append('turn/end',{turn:1,reason:{kind:'completed'}});
 assert.deepEqual(turns,[{phase:'task-start',turn:1},{phase:'turn-end',reason_kind:'completed',turn:1}]);
 console.log(JSON.stringify({registry:'actual pinned ToolRuntime',positive_writes:writes,
  denied_calls:events.filter(e=>e.phase==='tool-denied').length,outside_unchanged:true}));
} finally {await ctx.fiber.dispose();}
'''
        with tempfile.TemporaryDirectory(prefix='dsh-guard-spike-') as root:
            entry = Path(root) / 'fixture.mjs'
            entry.write_text(script)
            result = subprocess.run([node, str(entry), runtime, str(plugin), root],
                                    text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())


if __name__ == '__main__':
    unittest.main()
