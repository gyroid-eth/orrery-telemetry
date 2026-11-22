// Generated definitions through the pinned native adapter, using a loopback API only.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
import path from 'node:path';
import http from 'node:http';
import {once} from 'node:events';

const runtime = process.env.ORRERY_DSH_TEST_RUNTIME;
const skip = !runtime && 'explicit pinned native runtime required';
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jZioAAAAASUVORK5CYII=', 'base64');

function generatedProvider(model, context, modalities) {
  const code = `
import json, sys
sys.path.insert(0, 'tests')
from test_dsh_thin_config import ThinTests, thin
fixture = ThinTests()
fixture.setUp()
try:
    manifest = thin.read(fixture.manifest)
    manifest['backends']['strata'] = {'model_source': 'server', 'urls': ['http://127.0.0.1:1/v1']}
    thin.save(fixture.manifest, manifest)
    model, context, modalities = json.loads(sys.argv[1])
    plan = thin.generate({**fixture.doc, 'model': model, 'url_index': 0,
        'server_context': {'n_ctx': context}, 'server_input_modalities': modalities})
    print(json.dumps(next(row['config'] for row in fixture.rows(plan) if row.get('id') == 'llm-deepseek')))
finally:
    fixture.doCleanups()
`;
  const result = spawnSync(process.env.ORRERY_DSH_TEST_PYTHON ?? 'python3',
    ['-c', code, JSON.stringify([model, context, modalities])], {encoding: 'utf8', timeout: 10000});
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

for (const [model, context] of [['deepseek-v4.1-flash', 131072], ['glm-5.3-flash', 262144]]) {
  for (const [modalities, expected] of [
    [['text', 'image'], ['text', 'image']], [['text', 'image', 'video'], ['text', 'image']],
    [['text'], ['text']], [null, ['text']], [['image'], ['text']],
  ]) {
    test(`${model}: generated ${JSON.stringify(modalities)} reaches native image transport`, {skip}, async () => {
      const require = createRequire(path.join(path.resolve(runtime), 'package.json'));
      const {DeepSeekAdapter, resolveAdapterOptions} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-llm-deepseek')));
      const provider = generatedProvider(model, context, modalities);
      const received = [];
      const server = http.createServer(async (req, res) => {
        let body = ''; for await (const part of req) body += part;
        received.push({url: req.url, body: JSON.parse(body)});
        const events = [
          {type: 'message_start', message: {id: 'fixture', type: 'message', role: 'assistant', content: [], model, usage: {input_tokens: 1, output_tokens: 0}}},
          {type: 'content_block_start', index: 0, content_block: {type: 'text', text: ''}},
          {type: 'content_block_delta', index: 0, delta: {type: 'text_delta', text: 'fixture-ok'}},
          {type: 'content_block_stop', index: 0},
          {type: 'message_delta', delta: {stop_reason: 'end_turn', stop_sequence: null}, usage: {output_tokens: 1}},
          {type: 'message_stop'},
        ];
        res.writeHead(200, {'content-type': 'text/event-stream'});
        res.end(events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(''));
      });
      const fetch0 = globalThis.fetch;
      try {
        server.listen(0, '127.0.0.1'); await once(server, 'listening');
        const baseURL = `http://127.0.0.1:${server.address().port}/anthropic`;
        globalThis.fetch = (url, options) => {
          assert.ok(String(url).startsWith(baseURL + '/'), 'external network forbidden');
          return fetch0(url, options);
        };
        const options = resolveAdapterOptions({...provider, baseURL});
        const attachment = {attachmentId: 'fixture-image', mediaType: 'image/png',
          bytes: png.length, width: 1, height: 1, name: 'fixture.png'};
        let reads = 0;
        const adapter = new DeepSeekAdapter({options: () => options,
          resolveAuth: async () => ({headers: {'x-api-key': 'synthetic-key'}}),
          resolveUserId: () => 'fixture-user',
          prepareExtensions: async () => ({fields: {}, accept: async () => {}}),
          resolveAttachments: () => ({readImageRequest: async () => {
            reads++;
            return {variantId: 'fixture-variant', attachment, data: png, mediaType: 'image/png',
              bytes: png.length, width: 1, height: 1, depth: 'uchar', space: 'srgb', hasAlpha: true};
          }}),
          // A backend without Files API uses the native adapter's inline fallback.
          resolveFiles: () => ({ensureUploaded: async () => {throw Error('fixture has no Files API');}}),
        });
        const info = await adapter.resolveModel('deepseek-official', model);
        assert.deepEqual(info.inputModalities, expected);
        assert.equal(info.context.contextWindow, context);
        assert.equal(info.defaultMaxTokens, 8192);
        const send = async () => {
          for await (const event of adapter.stream({provider: 'deepseek-official', model, maxTokens: 8,
            messages: [{role: 'user', content: [{type: 'text', text: 'Describe this image'}, {type: 'image', attachment}]}]})) {}
        };
        if (expected.includes('image')) {
          await send();
          assert.equal(reads, 1);
          assert.equal(received.length, 1);
          assert.equal(received[0].url, '/anthropic/v1/messages');
          assert.equal(received[0].body.model, model);
          const image = received[0].body.messages[0].content.find(part => part.type === 'image');
          assert.equal(image.source.media_type, 'image/png');
          assert.equal(image.source.data, png.toString('base64'));
        } else {
          await assert.rejects(send(), error => error.code === 'UNSUPPORTED_CONTENT');
          assert.equal(reads, 0);
          assert.equal(received.length, 0);
        }
      } finally {
        globalThis.fetch = fetch0;
        await new Promise(resolve => server.close(resolve));
      }
    });
  }
}
