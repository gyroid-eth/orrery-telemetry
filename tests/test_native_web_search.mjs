// 偽 HTTP だけを使う。固定版 runtime は明示したものだけを読む。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { pathToFileURL } from 'node:url';
import { apply, SearXNGProvider } from '../integrations/dsh/web_search.mjs';

async function server(t, payload) {
  const calls = [];
  const app = http.createServer(async (req, res) => {
    let body = '';
    for await (const part of req) body += part;
    calls.push({ url: req.url, method: req.method, body, authorization: req.headers.authorization });
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(payload));
  });
  app.listen(0, '127.0.0.1');
  await once(app, 'listening');
  t.after(() => new Promise(resolve => app.close(resolve)));
  return { url: `http://127.0.0.1:${app.address().port}`, calls, app };
}

test('SearXNG: 偽 HTTP の URL・title・content を渡し、停止をエラーにする', async t => {
  const fake = await server(t, { results: [{ title: '見本', url: 'https://example.invalid/source', content: '本文' }] });
  let provider;
  await apply({ web: { registerSearchProvider(p) { provider = p; } } }, { provider: 'searxng', url: fake.url });
  assert.equal(provider.available(), true);
  assert.deepEqual((await provider.search({ query: '日本語 & query' })).sources,
    [{ title: '見本', url: 'https://example.invalid/source', snippet: '本文' }]);
  const query = new URL(fake.calls[0].url, fake.url).searchParams;
  assert.equal(query.get('q'), '日本語 & query');
  assert.equal(query.get('format'), 'json');
  await new Promise(resolve => fake.app.close(resolve));
  assert.equal(provider.available(), true);
  await assert.rejects(provider.search({ query: '停止後' }), /SearXNG/);
});

test('SearXNG: 200・0件でも応答しなかった engine を知らせる', async t => {
  const fake = await server(t, { results: [], unresponsive_engines: [['fixture-engine', 'blocked']] });
  await assert.rejects(new SearXNGProvider(fake.url).search({ query: 'query' }), /fixture-engine: blocked/);
});

test('Exa: 上流の固定版で偽 HTTP を呼び、鍵無しは検索時だけ失敗する',
  { skip: !process.env.ORRERY_DSH_TEST_RUNTIME }, async t => {
    const runtime = process.env.ORRERY_DSH_TEST_RUNTIME;
    const fake = await server(t, { results: [{ title: '見本', url: 'https://example.invalid/exa', highlights: ['要約'] }] });
    let provider;
    const ctx = { web: { registerSearchProvider(p) { provider = p; } } };
    const config = { provider: 'exa', runtime, baseURL: fake.url,
      reader_command: [process.execPath, '-e', 'process.stdout.write("fixture-exa-token\\n")'] };
    await apply(ctx, config);
    const providers = new Map([[provider.id, provider], ['deepseek-official', { available: () => true, search() { throw Error('別の検索先'); } }]]);
    const { WebRuntime } = await import(pathToFileURL(`${runtime}/node_modules/@deepseek-ai/dsh-web/lib/index.js`));
    const web = { searchProviders: providers, config: { searchProvider: { get: () => 'orrery-exa' } } };
    web.search = (request, signal) => WebRuntime.prototype.search.call(web, request, signal);
    const { applyWebSearchTool } = await import(pathToFileURL(`${runtime}/node_modules/@deepseek-ai/dsh-tool-web/lib/index.js`));
    let tool;
    applyWebSearchTool({ web, tools: { register(t) { tool = t; } }, systemPrompt: { section() {}, getSectionOrder() { return 1; } } }, 5, 4, 30000, false);
    const result = await tool.execute({ queries: ['fixture'] }, { signal: new AbortController().signal });
    assert.equal(result.sources[0].url, 'https://example.invalid/exa');
    assert.equal(fake.calls[0].authorization, 'Bearer fixture-exa-token');
    assert.equal(JSON.parse(fake.calls[0].body).query, 'fixture');
    await apply(ctx, { ...config, reader_command: [process.execPath, '-e', 'process.exit(1)'] });
    assert.equal(provider.available(), true);
    await assert.rejects(provider.search({ query: '鍵無し' }), /Exa.*資格/);
  });
