"""同じ描画関数でopt-inと明示local入力を対照する。通信・鍵は使わない。"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import dashboard.server as server
from test_network_node import HARNESS

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'dashboard/index.html').read_text()


def _node(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is unavailable')
    result = subprocess.run([node, '-e', script], capture_output=True,
                            text=True, check=False, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _function(name):
    return re.search(r'^function ' + name + r'\([^\n]*\)\{.*?^}',
                     HTML, re.M | re.S).group(0)


def test_server_display_flag_follows_same_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'HERE', str(tmp_path))
    for enabled in (False, True):
        (tmp_path / 'dsh-provider.json').write_text(json.dumps({'enabled': enabled}))
        rendered = server._render_dashboard_index(server._DASHBOARD_CONFIG_MARKER)
        config = json.loads(rendered.decode().split('=', 1)[1].rstrip(';'))
        assert config.get('dshEnabled', False) is enabled
        if not enabled:
            assert config == {'language': None, 'murmur': None}
        assert server._provider_of('deepseek-3.2') == 'deepseek'


def test_network_cloud_off_on_and_explicit_local():
    script = HARNESS + (ROOT / 'dashboard/network_node.js').read_text() + '''
const n=window.OrreryNetworkNode;
function draw(g){return snapshot(n.render('Alpha',g,{avatar:()=>null}));}
const result=[];
for(const enabled of [false,true]){
 window.AGENTSTACK_DSH_ENABLED=enabled;
 result.push({enabled,cloud:draw({provider:'deepseek',model:'deepseek-3.2'}),
   localQwen:draw({provider:'local',model_raw:'Qwen/Qwen3-Coder',program:'codex'}),
   localDeepSeek:draw({provider:'local',model_raw:'deepseek-ai/DeepSeek-V3',program:'codex'}),
   unknown:draw({provider:'local',model_raw:'unknown',program:'codex'}),
   noLocal:draw({model:'Qwen3-Coder',program:'codex'})});
}
process.stdout.write(JSON.stringify(result));
'''
    def nodes(tree, tag):
        return ([tree] if tree['tag'] == tag else []) + [
            node for child in tree['children'] for node in nodes(child, tag)]
    off, on = _node(script)
    assert not nodes(off['cloud'], 'image')
    logo = nodes(on['cloud'], 'image')[0]['attrs']
    assert logo['data-provider'] == 'deepseek'
    assert float(logo['width']) <= 14 and float(logo['height']) <= 14
    for state in (off, on):
        local = state['localQwen']
        assert len(nodes(local, 'polygon')) == 1
        assert 'provider-local-ring' in nodes(local, 'polygon')[0]['attrs']['class']
        assert nodes(local, 'image')[0]['attrs']['data-provider'] == 'qwen'
        assert len(nodes(state['unknown'], 'polygon')) == 1
        assert not nodes(state['unknown'], 'image')
        assert not nodes(state['noLocal'], 'polygon')
        assert not nodes(state['noLocal'], 'image')
        assert len(nodes(state['localDeepSeek'], 'polygon')) == 1
    assert not nodes(off['localDeepSeek'], 'image')
    assert nodes(on['localDeepSeek'], 'image')[0]['attrs']['data-provider'] == 'deepseek'


def test_deck_usage_and_unconnected_spawn_consume_flags():
    logo_map = re.search(r'^const USAGE_LOGO=.*?;', HTML, re.M).group(0)
    script = '''global.window={};
function assetURL(p){return '/assets/'+p+'.svg';}
''' + logo_map + _function('deckProviderLogo') + _function('usageLogo') + '''
let spmProviders=[{id:'dsh',modelNote:'未接続'}],spmSelectedProvider='dsh';
let spmSelectedModel='deepseek-flash',spmSelectedEffort='low';
function spawnSelectedProviderCapabilities(){return {runtime:false};}
function SPM(){return null;}
''' + _function('spawnProviderRequirementsMissing') + '''
const out=[];
for(const enabled of [false,true]){
 window.AGENTSTACK_DSH_ENABLED=enabled;
 out.push({enabled,deck:deckProviderLogo('deepseek','on'),usage:usageLogo('dsh',''),
    claude:deckProviderLogo('anthropic','on'),blocked:spawnProviderRequirementsMissing()});
}
process.stdout.write(JSON.stringify(out));
'''
    off, on = _node(script)
    assert off['deck'] == off['usage'] == ''
    assert '/assets/deepseek.svg' in on['deck']
    assert '/assets/deepseek.svg' in on['usage']
    assert off['claude'] == on['claude']
    assert off['blocked'] == on['blocked'] == '未接続'


def test_demo_build_explicit_opt_in_keeps_default_empty(tmp_path):
    for enabled in (False, True):
        out = tmp_path / ('enabled' if enabled else 'default')
        args = ['/bin/bash', str(ROOT / 'dashboard/demo/build.sh'), str(out)]
        if enabled:
            args.append('--enable-dsh')
        subprocess.run(args, capture_output=True, text=True, check=True, timeout=30)
        assert (out / 'assets/deepseek.svg').exists() is enabled
        assert (out / 'assets/qwen.svg').read_bytes() == (ROOT / 'dashboard/assets/qwen.svg').read_bytes()
        marker = 'window.AGENTSTACK_DEMO_DSH=' + str(enabled).lower()
        assert marker in (out / 'index.html').read_text()
        # catalogも同じ配布fixtureを実行し、markerだけの対照にしない。
        js = '\n'.join(p.read_text() for p in sorted((out / 'demo').glob('story_*.js')))
        js += '\n' + (out / 'demo/demo_api.js').read_text()
        script = '''const vm=require('vm');const w={location:{search:'?demo=1'},URLSearchParams,
fetch:()=>{},dispatchEvent:()=>true,CustomEvent:function(){},Response:class{}};
w.window=w;w.AGENTSTACK_DEMO_DSH=''' + str(enabled).lower() + ''';
vm.createContext(w);vm.runInContext(''' + json.dumps(js) + ''',w);
process.stdout.write(JSON.stringify(w.AGENTSTACK_DEMO.payloads.spawnNames()));'''
        # fixtureの公開payload名はsourceのexportに従う。
        providers = _node(script)['providers']
        assert any(item['id'] == 'dsh' for item in providers) is enabled
