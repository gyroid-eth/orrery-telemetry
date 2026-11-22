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
   localGLM:draw({provider:'local',model_raw:'zai-org/GLM-5.3-Flash',program:'hermes'}),
   localGLM46:draw({provider:'local',model_raw:'zai-org/GLM-4.6'}),
   localXGLM:draw({provider:'local',model_raw:'facebook/xglm-7.5B'}),
   localChatGLM:draw({provider:'local',model_raw:'chatglm3-6b'}),
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
        glm = state['localGLM']
        assert len(nodes(glm, 'polygon')) == 1
        assert nodes(glm, 'image')[0]['attrs']['data-provider'] == 'glm'
        assert nodes(state['localGLM46'], 'image')[0]['attrs']['data-provider'] == 'glm'
        for name in ('localXGLM', 'localChatGLM'):
            assert len(nodes(state[name], 'polygon')) == 1
            assert not nodes(state[name], 'image')
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


def test_deck_and_usage_consume_flags():
    logo_map = re.search(r'^const USAGE_LOGO=.*?;', HTML, re.M).group(0)
    script = '''global.window={};
function assetURL(p){return '/assets/'+p+'.svg';}
function providerKeyFor(a){return a.provider;}
''' + logo_map + _function('deckProviderLogo') + _function('usageLogo') + '''
const out=[];
for(const enabled of [false,true]){
 window.AGENTSTACK_DSH_ENABLED=enabled;
 out.push({enabled,deck:deckProviderLogo({provider:'deepseek'},'on'),usage:usageLogo('dsh',''),
    claude:deckProviderLogo({provider:'anthropic'},'on')});
}
process.stdout.write(JSON.stringify(out));
'''
    off, on = _node(script)
    assert off['deck'] == off['usage'] == ''
    assert '/assets/deepseek.svg' in on['deck']
    assert '/assets/deepseek.svg' in on['usage']
    assert off['claude'] == on['claude']


def test_demo_build_explicit_opt_in_keeps_default_empty(tmp_path):
    for enabled in (False, True):
        out = tmp_path / ('enabled' if enabled else 'default')
        args = ['/bin/bash', str(ROOT / 'dashboard/demo/build.sh'), str(out)]
        if enabled:
            args.append('--enable-dsh')
        subprocess.run(args, capture_output=True, text=True, check=True, timeout=30)
        assert (out / 'assets/deepseek.svg').exists() is enabled
        assert (out / 'assets/qwen.svg').read_bytes() == (ROOT / 'dashboard/assets/qwen.svg').read_bytes()
        assert (out / 'assets/glm.svg').read_bytes() == (ROOT / 'dashboard/assets/glm.svg').read_bytes()
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
        assert all(item['id'] != 'dsh' for item in providers)


def test_strata_badges_share_local_metadata_in_network_deck_and_global():
    script = HARNESS + (ROOT / 'dashboard/network_node.js').read_text() + (ROOT / 'dashboard/runtime_node.js').read_text() + """
Object.defineProperty(El.prototype,'outerHTML',{get(){return '<'+this.tag+' '+Object.entries(this.attrs).map(([k,v])=>k+'="'+v+'"').join(' ')+'>'+this.children.map(c=>c.outerHTML).join('')+'</'+this.tag+'>';}});
const {providerKeyFor,providerBadge}=window.OrreryNetworkNode;
""" + _function('deckProviderLogo') + """
const rows=[
 {provider:'strata',locality:'local',program:'dsh',harness:'dsh',model:'Qwen 3.8 flash',model_raw:'qwen3.8-flash-next-iq3_xxs'},
 {provider:'strata',program:'hermes',model:'fixture',model_raw:'Qwen/Qwen3-Coder'},
 {provider:'fixture',locality:'local',model:'fixture',model_raw:'qwen3'},
 {provider:'strata',locality:'local',program:'hermes',model:'GLM 5.3 flash',model_raw:'glm-5.3-flash'},
 {provider:'local',model:'zai-org/GLM-5.3-Flash'},
 {provider:'local',model:'Qwen3',model_raw:'unknown'},
 {provider:'strata',model:'unknown'},
];
const out=[];
for(const enabled of [false,true]){
 window.AGENTSTACK_DSH_ENABLED=enabled;
 out.push(rows.map((row,i)=>{
  const globalRow=window.OrreryRuntimeNode.adapt([{...row,agent_id:i+1}],[]).get(i+1);
  return {key:providerKeyFor(row),network:snapshot(window.OrreryNetworkNode.render('Fixture',row,{avatar:()=>null})),
    deck:deckProviderLogo(row,'on'),globalKey:providerKeyFor(globalRow)};
 }));
}
process.stdout.write(JSON.stringify(out));
"""
    for state in _node(script):
        for i, row in enumerate(state):
            maker = ['qwen', 'qwen', 'qwen', 'glm', 'glm', None, None][i]
            assert row['key'] == row['globalKey'] == ('local:' + maker if maker else 'local')
            assert 'provider-local-ring' in json.dumps(row['network'])
            if maker:
                assert maker + '.svg' in json.dumps(row['network'])
            else:
                assert '.svg' not in json.dumps(row['network'])
            assert 'provider-local-ring' in row['deck']
            if maker:
                assert maker + '.svg' in row['deck']
            else:
                assert '.svg' not in row['deck']
            assert 'prov-logo' in row['deck'] and 'on' in row['deck']


def test_network_projection_keeps_locality_and_raw_model():
    assignment = re.search(r'Object.assign\(g,\{model:n.model,provider:n.provider.*?\}\);', HTML, re.S).group(0)
    script = 'const g={},n={model:"display",model_raw:"qwen3",provider:"strata",locality:"local"};' + assignment + 'process.stdout.write(JSON.stringify(g));'
    row = _node(script)
    assert row['model_raw'] == 'qwen3' and row['locality'] == 'local'


def test_network_badge_rebuilds_when_only_resolved_local_maker_changes():
    tick = HTML[HTML.index('async function netTick(){'):]
    tick = tick[:tick.index('\n}\n') + 2]
    script = HARNESS + (ROOT / 'dashboard/network_node.js').read_text() + """
const {providerKeyFor}=window.OrreryNetworkNode;
const info={innerHTML:'',textContent:''};
document.getElementById=()=>info;
const gmap=new Map(),gEls={node:new Map()};
let gWin='all',gWinLabel='',graphStateAt,gedges=[],gspawn=[],netRenderKey='';
let drag=null,pan=null,fitPending=false,viewUserAdjusted=true,simHot=0;
const netDims=()=>({w:800,h:600}),refreshPanelButtons=()=>{},esc=String;
const runSim=()=>{},fitView=()=>{},scheduleFit=()=>{};
const builds=[];
function buildEls(){builds.push(snapshot(window.OrreryNetworkNode.render('Fixture',gmap.get('Fixture'),{avatar:()=>null})));}
const row={name:'Fixture',provider:'fixture',model:'same display',model_raw:'qwen3',locality:'cloud'};
const fetch=async()=>({status:200,json:async()=>({nodes:[{...row}],edges:[],spawn:[]})});
""" + tick + """
(async()=>{
 const counts=[];
 for(const patch of [{},{locality:'local'},{model_raw:'unknown'},{},{model_raw:'qwen3'},{locality:'cloud'}]){
  Object.assign(row,patch);await netTick();counts.push(builds.length);
 }
 process.stdout.write(JSON.stringify({counts,builds,caught:info.textContent}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
    result = _node(script)
    assert result['caught'] == ''
    assert result['counts'] == [1, 2, 3, 3, 4, 5]
    for tree, local, qwen in zip(result['builds'], [False, True, True, True, False], [False, True, False, True, False]):
        dom = json.dumps(tree)
        assert ('provider-local-ring' in dom) is local
        assert ('qwen.svg' in dom) is qwen
