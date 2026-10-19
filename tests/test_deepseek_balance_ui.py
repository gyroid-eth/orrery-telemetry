"""既存usageの同じ描画経路でUSD残高とmasterのnull caseを対照する。"""
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/deepseek_usage_null.json').read_text())


def render(providers, enabled=True):
    html = (ROOT / 'dashboard/index.html').read_text()
    block = html.split('const USAGE_WARN=', 1)[1].split('async function usageTick()', 1)[0]
    script = '''
const elements={'usage-pill-list':{innerHTML:''},'usage-pill':{hidden:true,title:''},
'usage-body':{innerHTML:''},'usage-kick':{textContent:''}};
const document={getElementById:id=>elements[id]||null};
const localStorage={getItem:()=>null};
const window={AGENTSTACK_DSH_ENABLED:''' + str(enabled).lower() + '''};
const esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const assetURL=name=>`assets/${name}.svg`;
const USAGE_WARN=''' + block + '''
const providers=''' + json.dumps(providers) + ''';
renderUsagePill(providers);renderUsagePopover({providers});
console.log(JSON.stringify({pill:{html:elements['usage-pill-list'].innerHTML,
 title:elements['usage-pill'].title,hidden:elements['usage-pill'].hidden},
 popover:{html:elements['usage-body'].innerHTML,kick:elements['usage-kick'].textContent}}));
'''
    result = subprocess.run(['node', '-e', script], env={**os.environ, 'TZ': 'UTC'},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def row(balance, status='ok'):
    return {'provider': 'dsh', 'measurement_kind': 'balance', 'balance': balance,
            'status': status, 'buckets': [], 'observed_at': 1000, 'last_observed_at': 1000}


def test_opt_in_off_same_consumer_matches_master_exactly():
    current = render(FIXTURE['providers'], enabled=False)
    assert current == {key: FIXTURE[key] for key in ('pill', 'popover')}
    assert 'DeepSeek' not in json.dumps(current)


def test_usd_total_and_availability_in_existing_usage_provider_row():
    balance = {'currency': 'USD', 'total_balance': '12.3400', 'is_available': True}
    current = render(FIXTURE['providers'] + [row(balance)])
    assert 'USD 12.3400' in current['pill']['html']
    assert '利用可能' in current['pill']['html']
    assert 'USD 12.3400' in current['popover']['html']
    assert '利用可能' in current['popover']['html']
    assert 'CNY' not in json.dumps(current)
    assert 'deepseek.svg' in current['popover']['html']
    assert FIXTURE['popover']['html'] in current['popover']['html']
    assert 'up-dial' not in render([row(balance)])['popover']['html']


def test_unavailable_account_is_warning_not_acquisition_failure():
    current = render([row({'currency': 'USD', 'total_balance': '0.0000', 'is_available': False})])
    assert 'USD 0.0000' in current['popover']['html']
    assert '利用不可' in current['popover']['html']
    assert 'up-balance low' in current['popover']['html']
    assert 'up-p low' in current['pill']['html']


def test_no_usd_and_fetch_failure_are_distinct_and_never_fake_zero():
    missing = render([row({'currency': 'USD', 'total_balance': None, 'is_available': True})])
    assert 'USD の残高なし' in missing['popover']['html']
    failed = render([row(None, 'unavailable')])
    assert '不明' in failed['popover']['html'] and '不明' in failed['pill']['html']
    assert 'USD 0' not in json.dumps(failed)
    assert '利用不可' not in failed['popover']['html']
    assert failed['pill']['hidden'] is False
