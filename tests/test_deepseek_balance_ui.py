"""既存usageの同じ描画経路でUSD残高とmasterのnull caseを対照する。"""
import json
import os
import re
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/deepseek_usage_null.json').read_text())


def render(providers, enabled=True, failure=None):
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
const original=JSON.stringify(providers);
renderUsagePill(providers);renderUsagePopover({providers});
if(JSON.stringify(providers)!==original)throw new Error('表示がAPI/cacheの原精度を変更した');
console.log(JSON.stringify({pill:{html:elements['usage-pill-list'].innerHTML,
 title:elements['usage-pill'].title,hidden:elements['usage-pill'].hidden},
 popover:{html:elements['usage-body'].innerHTML,kick:elements['usage-kick'].textContent}}));
'''
    if failure:
        tick = re.search(r'async function usageTick\(\)\{.*?^}', html, re.M | re.S).group(0)
        script = script.split('const original=JSON.stringify(providers);', 1)[0] + tick + '''
let reads=0;
const original=JSON.stringify(providers);
const failure=''' + json.dumps(failure) + ''';
const fetch=async()=>{
 if(reads++===0)return {ok:true,json:async()=>({providers})};
 if(failure==='fetch')throw new Error('offline');
 if(failure==='http')return {ok:false};
 return {ok:true,json:async()=>{
  if(failure==='json')throw new Error('invalid JSON');
  return {error:'unexpected shape'};
 }};
};
const snapshot=()=>({pill:elements['usage-pill-list'].innerHTML,
 popover:elements['usage-body'].innerHTML,providers:usageLast.providers});
(async()=>{
 await usageTick();const before=snapshot();
 await usageTick();
 if(JSON.stringify(providers)!==original)throw new Error('APIの観測値を変更した');
 console.log(JSON.stringify({before,after:snapshot()}));
})();
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
    assert '$12' in current['pill']['html']
    assert 'Account available' not in current['pill']['html']
    assert '$12.34' in current['popover']['html']
    assert 'Account available' in current['popover']['html']
    assert 'Account available' in current['pill']['title']
    assert current['popover']['kick'].endswith('numbers are % left; balances in dollars')
    assert 'CNY' not in json.dumps(current)
    assert 'deepseek.svg' in current['popover']['html']
    assert FIXTURE['popover']['html'] in current['popover']['html']
    assert 'up-dial' not in render([row(balance)])['popover']['html']


def test_unavailable_account_is_warning_not_acquisition_failure():
    current = render([row({'currency': 'USD', 'total_balance': '0.0000', 'is_available': False})])
    assert '$0.00' in current['popover']['html']
    assert 'Account unavailable' in current['popover']['html']
    assert 'up-balance low' in current['popover']['html']
    assert 'up-p low' in current['pill']['html']


def test_no_usd_and_fetch_failure_are_distinct_and_never_fake_zero():
    missing = render([row({'currency': 'USD', 'total_balance': None, 'is_available': True})])
    assert 'No USD balance' in missing['popover']['html']
    failed = render([row(None, 'unavailable')])
    assert '$?' in failed['popover']['html'] and '$?' in failed['pill']['html']
    assert '$0' not in json.dumps(failed)
    assert 'Account unavailable' not in failed['popover']['html']
    assert failed['pill']['hidden'] is False


@pytest.mark.parametrize('raw,pill,popover', [
    ('12.349', '$12', '$12.34'),
    ('12.3400', '$12', '$12.34'),
    ('0.009', '$0', '$0.00'),
    ('0.99', '$0', '$0.99'),
    ('12.3', '$12', '$12.30'),
    ('999999999999999.999', '$999999999999999', '$999999999999999.99'),
    ('-0.009', '$-1', '$-0.01'),
])
def test_usd_display_floors_each_surface_without_changing_raw_precision(raw, pill, popover):
    balance = {'currency': 'USD', 'total_balance': raw, 'is_available': True}
    current = render([row(balance)])
    assert f'<b>{pill}</b>' in current['pill']['html']
    assert f'<span>{popover}</span>' in current['popover']['html']
    assert balance['total_balance'] == raw


@pytest.mark.parametrize('failure', ['http', 'fetch', 'json', 'schema'])
def test_usage_tick_failure_clears_only_balance_after_a_success(failure):
    providers = FIXTURE['providers'] + [row({'currency': 'USD', 'total_balance': '12.349', 'is_available': True})]
    result = render(providers, failure=failure)
    assert '$12.34' in result['before']['popover']
    assert 'Account available' in result['before']['popover']
    assert '$?' in result['after']['popover'] and '$?' in result['after']['pill']
    assert '$12' not in result['after']['popover']
    assert 'Account available' not in result['after']['popover']
    assert result['after']['providers'][:-1] == providers[:-1]
    assert FIXTURE['popover']['html'] in result['after']['popover']
    assert result['after']['providers'][-1]['balance'] is None
