"""native の SearXNG に、応答しなかった engine の判定を足す。"""
import logging

from plugins.web.searxng.provider import SearXNGWebSearchProvider
from plugins.web._common import http_get_json, provider_env, search_fail, search_ok, titled_rows


class SearXNGProvider(SearXNGWebSearchProvider):
    NAME = 'orrery-searxng'

    def search(self, query, limit=5):
        url = provider_env('SEARXNG_URL').rstrip('/')
        if not url:
            return search_fail('SEARXNG_URL が設定されていません')
        data, failure = http_get_json('SearXNG', url + '/search',
            params={'q': query, 'format': 'json', 'pageno': 1},
            headers={'Accept': 'application/json'}, timeout=15,
            logger=logging.getLogger(__name__), reach_target='SearXNG')
        if failure is not None:
            return failure
        rows = data.get('results', [])
        if not rows and data.get('unresponsive_engines'):
            return search_fail('SearXNG で応答しなかった engine: ' +
                ', '.join(': '.join(map(str, e)) if isinstance(e, list) else str(e)
                          for e in data['unresponsive_engines']))
        rows = sorted(rows, key=lambda r: float(r.get('score', 0)), reverse=True)[:limit]
        return search_ok(titled_rows(rows, 'content'))


def register(ctx):
    ctx.register_web_search_provider(SearXNGProvider())
