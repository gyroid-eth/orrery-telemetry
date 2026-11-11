// 検索先だけを接続する。資格は環境変数・設定ファイルに置かない。
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const execute = promisify(execFile);
export const inject = ['web'];

export class SearXNGProvider {
  id = 'orrery-searxng';
  constructor(url) { this.url = url; }
  available() { return true; }
  async search(request, signal) {
    const url = new URL(`${this.url}/search`);
    url.searchParams.set('q', request.query);
    url.searchParams.set('format', 'json');
    let response;
    try {
      response = await fetch(url, { signal, redirect: 'error' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!Array.isArray(data.results)) throw new Error('results がありません');
      if (!data.results.length && data.unresponsive_engines?.length)
        throw new Error(`応答しなかった engine: ${data.unresponsive_engines.map(e => Array.isArray(e) ? e.join(': ') : String(e)).join(', ')}`);
      return { sources: data.results.map(r => ({ title: r.title, url: r.url, snippet: r.content || '' })), truncated: false };
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new Error(`SearXNG の検索に失敗しました: ${error.message}`, { cause: error });
    }
  }
}

export async function apply(ctx, config) {
  if (config.provider === 'searxng') {
    ctx.web.registerSearchProvider(new SearXNGProvider(config.url));
    return;
  }
  const module = pathToFileURL(path.join(config.runtime, 'node_modules/@deepseek-ai/dsh-web-search-exa/lib/index.js'));
  const { ExaSearchProvider } = await import(module.href);
  ctx.web.registerSearchProvider({
    id: 'orrery-exa',
    available: () => true,
    async search(request, signal) {
      let key;
      try {
        const { stdout } = await execute(config.reader_command[0], config.reader_command.slice(1),
          { timeout: 20000, maxBuffer: 65536, signal });
        key = stdout.trimEnd();
        if (!key) throw new Error('empty');
      } catch {
        throw new Error('Exa の資格を取得できません。web_search.exa.credential_ref を確認してください。');
      }
      return new ExaSearchProvider({ apiKey: key, baseURL: config.baseURL || 'https://api.exa.ai',
        searchType: 'auto', highlightsPerResult: 1 }).search(request, signal);
    },
  });
}
