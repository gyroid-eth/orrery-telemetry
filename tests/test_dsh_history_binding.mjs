import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import {saveHistoryBinding, apply} from '../integrations/dsh/orrery_tools.mjs';

function fixture() {
  const runtime = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-history-')));
  const state = path.join(runtime, 'child-agents/OwnDsh.harness', 'a'.repeat(32));
  fs.mkdirSync(state, {recursive:true});
  const home = path.join(runtime, 'native/dsh/OwnDsh');
  const launch = {schema_version:1, provider:'dsh', program:'dsh', agent_name:'OwnDsh',
    agent_id:7, project_key:'/fixture/project', launch_id:'b'.repeat(32), state_dir:state, native_home:home};
  const index = path.join(runtime, 'dsh-history/OwnDsh.json');
  fs.mkdirSync(path.dirname(index), {recursive:true});
  fs.writeFileSync(index, JSON.stringify(launch));
  const config = {state_dir:state, cwd:runtime, hooks_dir:runtime, history_binding:launch, env:{AGENTSTACK_RUNTIME_MODE:'legacy'}};
  const agent = {session:{id:'session-own', header:{id:'session-own', cwd:runtime}}};
  const file = path.join(home, 'sessions/workspace/session-own/session.v4.jsonl.zstd');
  const persistence = {locate:() => ({kind:'jsonl',path:file}), flush:async () => {
    fs.mkdirSync(path.dirname(file), {recursive:true}); fs.writeFileSync(file, 'synthetic artifact');
  }};
  const receipt = path.join(state, 'dsh-history.json');
  return {runtime, state, home, launch, index, config, agent, persistence, file, receipt,
    clean:() => fs.rmSync(runtime,{recursive:true,force:true})};
}

function binding(f) {
  return JSON.parse(execFileSync(process.env.DSH_HISTORY_TEST_PYTHON ?? 'python3', ['-c',
    'import json,sys; from dashboard.dsh_history import binding; print(json.dumps(binding(sys.argv[1], "OwnDsh", json.loads(sys.argv[2]))))',
    f.runtime, JSON.stringify(f.launch)], {encoding:'utf8'}));
}

test('failed first root observation keeps the launch unbound after writes recover', async t => {
  const f=fixture();
  try {
    const observed=path.join(f.state,`dsh-history-${f.launch.launch_id}.root.json`);
    const write=fs.writeFileSync;
    const mock=t.mock.method(fs,'writeFileSync',(file,...args)=>{
      if (file === observed) throw Object.assign(Error('root observation denied'),{code:'EACCES'});
      return write(file,...args);
    });
    // Both the independent observer and the persistence callback encounter the outage.
    await saveHistoryBinding(f.agent,f.config);
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    mock.mock.restore();
    const otherFile=path.join(f.home,'sessions/workspace/other-root/session.v4.jsonl.zstd');
    await saveHistoryBinding({session:{id:'other-root',header:{id:'other-root'}}},f.config,{
      locate:()=>({kind:'jsonl',path:otherFile}),flush:async()=>{
        fs.mkdirSync(path.dirname(otherFile),{recursive:true});fs.writeFileSync(otherFile,'synthetic other root');
      },
    });
    assert.equal(binding(f).history_binding,'unbound');
    assert.equal(binding(f).transcript_path,null);
    assert.ok(fs.existsSync(path.join(f.state,`dsh-history-${f.launch.launch_id}.lock`)));
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    assert.equal(binding(f).history_binding,'unbound','I/O recovery cannot revive the poisoned nonce');
    // A formal new launch recovers; delayed callbacks from the poisoned launch do not affect it.
    const next={...f.launch,launch_id:'c'.repeat(32)};
    fs.writeFileSync(f.index,JSON.stringify(next));
    await saveHistoryBinding(f.agent,{...f.config,history_binding:next},f.persistence);
    assert.equal(binding(f).history_binding,'bound');
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    assert.equal(binding(f).history_binding,'bound');
  } finally {f.clean();}
});

test('contender marker write failure prevents the owner from publishing or unlocking', async t => {
  const f=fixture();let release,first;
  try {
    const gate=new Promise(resolve=>{release=resolve;});
    first=saveHistoryBinding(f.agent,f.config,{...f.persistence,flush:()=>gate});
    const conflict=path.join(f.state,`dsh-history-${f.launch.launch_id}.conflict`);
    const write=fs.writeFileSync;
    const mock=t.mock.method(fs,'writeFileSync',(file,...args)=>{
      if (file === conflict) throw Object.assign(Error('conflict marker full'),{code:'ENOSPC'});
      return write(file,...args);
    });
    await saveHistoryBinding({session:{id:'other-root',header:{id:'other-root'}}},f.config,f.persistence);
    assert.equal(binding(f).history_binding,'unbound');
    release();await first;
    assert.equal(binding(f).history_binding,'unbound');
    assert.equal(binding(f).transcript_path,null);
    assert.equal(fs.existsSync(f.receipt),false,'the owner must not issue a receipt');
    assert.ok(fs.existsSync(path.join(f.state,`dsh-history-${f.launch.launch_id}.lock`)));
    mock.mock.restore();
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    assert.equal(binding(f).history_binding,'unbound');
    const next={...f.launch,launch_id:'c'.repeat(32)};
    fs.writeFileSync(f.index,JSON.stringify(next));
    await saveHistoryBinding(f.agent,{...f.config,history_binding:next},f.persistence);
    assert.equal(binding(f).history_binding,'bound');
  } finally {release?.();await first;f.clean();}
});

for (const failure of ['missing-locator', 'locator-error', 'invalid-location', 'flush', 'stat', 'empty', 'receipt-write']) {
  test(`a second root stays conflicted after ${failure} failure, including a later first-root callback`, async t => {
    const f = fixture();
    try {
      await saveHistoryBinding(f.agent,f.config,f.persistence);
      assert.equal(binding(f).history_binding,'bound');
      const other = {session:{id:'other-root',header:{id:'other-root'}}};
      let persistence = {...f.persistence};
      if (failure === 'missing-locator') delete persistence.locate;
      if (failure === 'locator-error') persistence.locate=() => {throw Error('locator failed');};
      if (failure === 'invalid-location') persistence.locate=() => ({kind:'unsupported'});
      if (failure === 'flush') persistence.flush=async () => {throw Error('flush failed');};
      if (failure === 'stat') persistence.locate=() => ({kind:'jsonl',path:f.file+'-missing'});
      if (failure === 'empty') {
        const empty=f.file+'-empty';fs.writeFileSync(empty,'');
        persistence={locate:()=>({kind:'jsonl',path:empty}),flush:async()=>{}};
      }
      if (failure === 'receipt-write') {
        const write=fs.writeFileSync;
        t.mock.method(fs,'writeFileSync',(file,...args) => {
          if (String(file).startsWith(f.receipt+'.')) throw Error('receipt write failed');
          return write(file,...args);
        });
      }
      await saveHistoryBinding(other,f.config,persistence);
      assert.equal(binding(f).history_binding,'unbound');
      assert.equal(binding(f).transcript_path,null);
      await saveHistoryBinding(f.agent,f.config,f.persistence);
      assert.equal(binding(f).history_binding,'unbound','the original root must not revive the old binding');
    } finally {f.clean();}
  });
}

test('root observation survives a failed first flush and a later different root', async () => {
  const f=fixture();
  try {
    await saveHistoryBinding(f.agent,f.config,{...f.persistence,flush:async()=>{throw Error('first flush failed');}});
    const otherFile=path.join(f.home,'sessions/workspace/other-root/session.v4.jsonl.zstd');
    await saveHistoryBinding({session:{id:'other-root',header:{id:'other-root'}}},f.config,{
      locate:()=>({kind:'jsonl',path:otherFile}),flush:async()=>{
        fs.mkdirSync(path.dirname(otherFile),{recursive:true});fs.writeFileSync(otherFile,'synthetic other root');
      },
    });
    assert.equal(binding(f).history_binding,'unbound');
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    assert.equal(binding(f).history_binding,'unbound');
  } finally {f.clean();}
});

test('root observation still runs when the persistence service cannot be injected', async () => {
  const f=fixture();
  try {
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    const handlers=new Map();
    apply({on:(name,fn)=>{const rows=handlers.get(name)??[];rows.push(fn);handlers.set(name,rows);},
      inject:()=>{}},f.config,async()=>({ok:true}));
    for (const fn of handlers.get('agent/created'))
      await fn({agent:{session:{id:'other-root',header:{id:'other-root'}}}});
    assert.equal(binding(f).history_binding,'unbound');
  } finally {f.clean();}
});

test('native locator + materialization issue an exact descriptor receipt', async () => {
  const f = fixture();
  try {
    await saveHistoryBinding(f.agent, f.config, f.persistence);
    const claim = JSON.parse(fs.readFileSync(f.receipt));
    assert.equal(claim.session_id,'session-own');
    assert.equal(claim.transcript_path,f.file);
    assert.equal(claim.inode,fs.statSync(f.file).ino);
    assert.equal(claim.launch_id,f.launch.launch_id);
    assert.equal(claim.conflicted,false);
    assert.match(claim.created_at,/Z$/);
    assert.equal(fs.statSync(f.receipt).mode & 0o777,0o600);
  } finally {f.clean();}
});

test('later callbacks preserve the original receipt and cannot rebind a replaced inode', async () => {
  const f=fixture();
  try {
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    const original=fs.readFileSync(f.receipt,'utf8');
    const replacement=f.file+'.replacement';
    fs.writeFileSync(replacement,'replacement artifact');
    fs.renameSync(replacement,f.file);
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    assert.equal(fs.readFileSync(f.receipt,'utf8'),original);
    assert.equal(binding(f).history_binding,'unbound');
    assert.equal(binding(f).history_binding_reason_code,'artifact_changed');
  } finally {f.clean();}
});

test('different top-level native IDs conflict; subagent cannot overwrite', async () => {
  const f = fixture();
  try {
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    const sub = {session:{id:'sub',header:{id:'sub',parentSession:'session-own'}}};
    await saveHistoryBinding(sub,f.config,f.persistence);
    assert.equal(JSON.parse(fs.readFileSync(f.receipt)).session_id,'session-own');
    const other = {session:{id:'other',header:{id:'other'}}};
    await saveHistoryBinding(other,f.config,f.persistence);
    assert.ok(fs.existsSync(path.join(f.state,`dsh-history-${f.launch.launch_id}.conflict`)));
    assert.equal(binding(f).history_binding,'unbound');
  } finally {f.clean();}
});

test('concurrent claims leave a permanent launch conflict marker', async () => {
  const f = fixture();
  try {
    await Promise.all([saveHistoryBinding(f.agent,f.config,f.persistence),saveHistoryBinding(f.agent,f.config,f.persistence)]);
    assert.ok(fs.existsSync(path.join(f.state,`dsh-history-${f.launch.launch_id}.conflict`)));
  } finally {f.clean();}
});

test('stale launcher cannot claim a new resume expectation', async () => {
  const f = fixture();
  try {
    const next={...f.launch,launch_id:'c'.repeat(32)};
    fs.writeFileSync(f.index,JSON.stringify(next));
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    assert.equal(fs.existsSync(f.receipt),false);
    await saveHistoryBinding(f.agent,{...f.config,history_binding:next},f.persistence);
    assert.equal(binding(f).history_binding,'bound');
    await saveHistoryBinding({session:{id:'old-other-root',header:{id:'old-other-root'}}},f.config,f.persistence);
    assert.equal(binding(f).history_binding,'bound');
    assert.equal(JSON.parse(fs.readFileSync(f.receipt)).launch_id,next.launch_id);
  } finally {f.clean();}
});

test('an unwritable conflict marker retains the reader-blocking launch lock', async t => {
  const f=fixture();
  try {
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    const conflict=path.join(f.state,`dsh-history-${f.launch.launch_id}.conflict`);
    const write=fs.writeFileSync;
    t.mock.method(fs,'writeFileSync',(file,...args)=>{
      if (file === conflict) throw Error('conflict marker write failed');
      return write(file,...args);
    });
    await saveHistoryBinding({session:{id:'other-root',header:{id:'other-root'}}},f.config,f.persistence);
    assert.equal(binding(f).history_binding,'unbound');
    assert.ok(fs.existsSync(path.join(f.state,`dsh-history-${f.launch.launch_id}.lock`)));
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    assert.equal(binding(f).history_binding,'unbound');
  } finally {f.clean();}
});

test('unavailable locator, flush failure and mismatched header stay unbound', async () => {
  const f = fixture();
  try {
    for (const persistence of [{}, {...f.persistence,flush:async () => {throw Error('unavailable');}}]) {
      await saveHistoryBinding(f.agent,f.config,persistence);
      assert.equal(fs.existsSync(f.receipt),false);
    }
    await saveHistoryBinding({session:{id:'other',header:{id:'session-own'}}},f.config,f.persistence);
    assert.equal(fs.existsSync(f.receipt),false);
  } finally {f.clean();}
});

test('fresh callbacks keep the protected hook and never inject legacy history', async () => {
  const f = fixture();
  try {
    const handlers = new Map(); let hooked = false;
    f.config.env.AGENTSTACK_RUNTIME_MODE='fresh';
    apply({on:(n,fn)=>handlers.set(n,fn),inject:()=>assert.fail('legacy dependency in fresh')},f.config,
      async () => {hooked=true;return {ok:true};});
    await handlers.get('agent/created')({agent:f.agent});
    assert.equal(hooked,true);
    await saveHistoryBinding(f.agent,f.config,f.persistence);
    assert.equal(fs.existsSync(f.receipt),false);
  } finally {f.clean();}
});

test('installed native backend + real Cordis injection, no model/provider call', {
  skip:!process.env.DSH_HISTORY_TEST_RUNTIME,
}, async () => {
  const f = fixture(); let root,handle;
  try {
    const modules = path.join(process.env.DSH_HISTORY_TEST_RUNTIME,'node_modules');
    const {createRequire} = await import('node:module');
    const require = createRequire(path.join(modules,'fixture.cjs'));
    const load = async pkg => import(pathToFileURL(require.resolve(pkg)).href);
    const {Context,Service} = await load('@deepseek-ai/cordis');
    const backend = await load('@deepseek-ai/dsh-session-persistence-jsonl');
    root = new Context();
    await root.plugin(ctx => {new Service(ctx,'tools');new Service(ctx,'workingDirectory');});
    await root.plugin(backend,{root:path.join(f.home,'sessions'),compression:'zstd',listConcurrency:1});
    const persistence=root.get('sessionPersistence');
    f.agent.session.header={version:4,id:'session-own',createdAt:Date.now(),cwd:f.runtime,isSeeded:false,delegationDepth:0};
    handle=await persistence.create(f.agent.session.header);
    await root.plugin({apply,inject:['tools','workingDirectory']},f.config);
    await root.serial('agent/created',{agent:f.agent});
    const receipt=JSON.parse(fs.readFileSync(f.receipt));
    assert.equal(receipt.transcript_path,persistence.locate(f.agent.session.header).path);
    assert.match(receipt.transcript_path,/session\.v4\.jsonl\.zstd$/);
    assert.equal(receipt.inode,fs.statSync(receipt.transcript_path).ino);
    const {zstdDecompressSync}=await import('node:zlib');
    const header=JSON.parse(zstdDecompressSync(fs.readFileSync(receipt.transcript_path)).toString());
    assert.equal(header.id,'session-own');
    assert.equal(header.type,'session');
  } finally {if(handle) await handle.close();if(root) await root.fiber.dispose();f.clean();}
});

for (const trigger of ['turn','checkpoint']) test(`installed TUI defers an empty startup, then ${trigger} binds its JSONL receipt`, {
  skip:!process.env.DSH_HISTORY_TEST_RUNTIME,
}, async t => {
  const f=fixture(); let root,handle;
  try {
    const {createRequire}=await import('node:module');
    const require=createRequire(path.join(process.env.DSH_HISTORY_TEST_RUNTIME,'fixture.cjs'));
    const load=async pkg=>import(pathToFileURL(require.resolve(pkg)).href);
    const {Context,Service}=await load('@deepseek-ai/cordis');
    const {LlmAdapter,createUserMessage}=await load('@deepseek-ai/dsh-llm');
    const tui=path.dirname(require.resolve('@deepseek-harness-tui/dsh-tui/package.json'));
    const {createFreshAgent}=await import(pathToFileURL(path.join(tui,'lib/types/dsh-adapter/fresh-agent.js')).href);
    root=new Context();
    await root.plugin(ctx=>{new Service(ctx,'workingDirectory');});
    for (const pkg of ['dsh-session','dsh-session-projection','dsh-llm','dsh-tools','dsh-system-prompt','dsh-agent','dsh-agent-loop']) {
      const module=await load('@deepseek-ai/'+pkg);
      await root.plugin(module.default??module,{});
    }
    await root.plugin(await load('@deepseek-ai/dsh-session-persistence-jsonl'),{
      root:path.join(f.home,'sessions'),compression:'zstd',listConcurrency:1,
    });
    // The shipped base checkpoints on session/created without awaiting it.
    // Include that real path: it can overlap our later agent/created observer.
    const storage=await load('@deepseek-ai/dsh-storage');
    await root.plugin(storage.default??storage,{});
    await root.plugin(await load('@deepseek-ai/dsh-storage-json'),{root:path.join(f.runtime,'storages')});
    const domain=await load('@deepseek-ai/dsh-storage-domain');
    await root.plugin(domain.default??domain,{backend:'json'});
    await root.plugin((await load('@deepseek-ai/dsh-session-projection-cache')).default,{
      writeEveryEvents:200,writeIntervalMs:5000,
    });
    class LocalReply extends LlmAdapter {
      async *stream() {
        yield {type:'block-start',index:0,blockType:'text'};
        yield {type:'text-delta',index:0,text:'fixture response'};
        yield {type:'block-end',index:0,block:{type:'text',text:'fixture response'}};
        yield {type:'finish',reason:{kind:'stop'}};
      }
    }
    root.llm.registerAdapter(['fixture'],new LocalReply());
    await root.plugin({apply,inject:['tools','workingDirectory']},f.config);
    handle=await createFreshAgent(root,root.agents,{sessionId:'session-own',meta:{cwd:f.runtime},
      agentOptions:{provider:'fixture',model:'fixture'}});
    const agent=handle.agent;
    const persistence=root.sessionPersistence;
    const file=persistence.locate(agent.session.header).path;
    const bytes=()=>fs.existsSync(file)?fs.statSync(file).size:0;
    assert.equal(bytes(),0,'the actual TUI gate must keep startup unmaterialized');
    assert.equal(binding(f).history_binding,'unbound');
    assert.equal(fs.existsSync(path.join(f.state,`dsh-history-${f.launch.launch_id}.conflict`)),false,
      'the startup checkpoint and root observer must not contend for their own lock');
    t.diagnostic(JSON.stringify({stage:'startup',bytes:bytes(),receipt:fs.existsSync(f.receipt)}));
    if (trigger==='turn') {
      const idle=new Promise(resolve=>root.on('agent/status',event=>{
        if (event.agent===agent && event.status==='idle') resolve();
      }));
      agent.followup(createUserMessage({content:[{type:'text',text:'synthetic regression turn'}],source:{kind:'user'}}));
      await Promise.race([idle,new Promise((_,reject)=>{
        const timer=setTimeout(()=>reject(Error('fixture turn did not finish')),5000);timer.unref();
      })]);
      assert.deepEqual(agent.session.snapshotEvents().find(event=>event.type==='turn/end').data.reason,{kind:'completed'});
      // Wait on the real writer, without invoking the session/flush retry.
      await persistence.flush();
      await new Promise(resolve=>setImmediate(resolve));
    } else {
      agent.session.append('session/title',{title:'synthetic checkpoint'});
      // Concurrent checkpoints must queue the same Session's receipt attempt.
      await Promise.all([root.sessions.flush(agent.session),root.sessions.flush(agent.session)]);
    }
    assert.ok(bytes()>0);
    t.diagnostic(JSON.stringify({stage:'after-'+trigger,bytes:bytes(),binding:binding(f).history_binding}));
    assert.equal(binding(f).history_binding,'bound');
    const receipt=fs.readFileSync(f.receipt,'utf8');
    await root.sessions.flush(agent.session);
    assert.equal(fs.readFileSync(f.receipt,'utf8'),receipt,'later checkpoints must preserve the issued receipt');
  } finally {if(handle) await handle.dispose();if(root) await root.fiber.dispose();f.clean();}
});
