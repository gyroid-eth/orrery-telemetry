// Local adapter metadata only: no credential, provider discovery or profile mount.
import {createRequire} from 'node:module';
import {pathToFileURL, fileURLToPath} from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

export async function extractEfforts(runtime, entries) {
  const require = createRequire(path.join(path.resolve(runtime), 'package.json'));
  const load = name => import(pathToFileURL(require.resolve(name)).href);
  const previous = globalThis.fetch;
  let attempted = 0;
  globalThis.fetch = async () => { attempted++; throw new Error('CAPABILITY_NETWORK_FORBIDDEN'); };
  let ctx;
  try {
    const [{Context}, {LlmRuntime}, {DeepSeekAdapter, resolveAdapterOptions}] = await Promise.all([
      load('@deepseek-ai/cordis'), load('@deepseek-ai/dsh-llm'), load('@deepseek-ai/dsh-llm-deepseek')]);
    ctx = new Context();
    new LlmRuntime(ctx);
    const result = [];
    for (const row of entries) {
      const config = {models: [{...(row.model_definition ?? {}), id: row.model}]};
      if (row.thinking !== undefined) config.thinking = row.thinking;
      // No reasoningEffort: a trial/profile override is not the harness default.
      const options = resolveAdapterOptions(config);
      const adapter = new DeepSeekAdapter({options: () => options});
      const dispose = ctx.llm.registerAdapter(['orrery-capabilities'], adapter);
      try {
        const info = await ctx.llm.resolveModelInfo('orrery-capabilities', row.model);
        const efforts = info.reasoning?.efforts?.map(item => item.id) ?? null;
        result.push({backend_id: row.backend_id, model: row.model,
          model_fingerprint: row.model_fingerprint,
          efforts, default_effort: info.reasoning?.defaultEffort ?? null,
          source: efforts === null ? 'unknown' : 'harness'});
      } finally { dispose(); }
    }
    if (attempted) throw new Error('CAPABILITY_NETWORK_FORBIDDEN');
    return {entries: result, forwarded: 0, network_attempts: attempted};
  } finally {
    if (ctx) await ctx.fiber.dispose();
    globalThis.fetch = previous;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const input = JSON.parse(fs.readFileSync(0, 'utf8'));
    const result = await extractEfforts(input.runtime, input.entries);
    process.stdout.write(JSON.stringify(result) + '\n');
  } catch {
    // Import/provider exceptions may contain private config; expose a stable code.
    process.stderr.write('CAPABILITY_EXTRACTION_FAILED\n');
    process.exitCode = 1;
  }
}
