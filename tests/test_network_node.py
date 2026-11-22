"""Shared glyph DOM matches the legacy renderer for all observable states."""
import json
import shutil
import subprocess
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
HARNESS="\nclass El {\n constructor(tag){this.tag=tag;this.attrs={};this.children=[];this.textContent='';this.classList={add:x=>this.attrs.class+=' '+x};}\n setAttribute(k,v){this.attrs[k]=String(v);}\n appendChild(x){this.children.push(x);return x;}\n insertBefore(x,before){this.children.splice(this.children.indexOf(before),0,x);}\n querySelectorAll(){return [];}\n addEventListener(){}\n}\nglobal.document={createElementNS:(ns,tag)=>new El(tag)};\nglobal.window={};Date.now=()=>0;\nconst cases=[\n {state:'run',running:true,role:'parent',actState:'work',ctxUsed:40,model:'codex'},\n {state:'run',running:true,role:'child',actState:'wait',ctxUsed:90,model:'claude'},\n {state:'run',running:true,role:'both',actState:'ask',provider:'google'},\n {state:'run',running:true,role:'none',actState:'question'},\n {state:'finished',role:'none'}, {state:'gone',role:'child'},\n {retired:true,role:'parent'}, {state:'unknown',role:'none'}, {state:'run',running:true,role:'none',requested_name:'Requested'}];\nfunction snapshot(e){return {tag:e.tag,attrs:e.attrs,text:e.textContent,children:e.children.map(snapshot)};}\n"
def test_shared_network_medallion_keeps_legacy_shape():
    node=shutil.which('node')
    if not node:pytest.skip('node is unavailable')
    script=HARNESS+(ROOT/'dashboard/network_node.js').read_text()+"process.stdout.write(JSON.stringify(cases.map(g=>snapshot(window.OrreryNetworkNode.render('Alpha',g,{avatar:()=>null})))));"
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,check=True,timeout=10)
    assert json.loads(result.stdout)==json.loads((ROOT/'tests/fixtures/network_node_legacy.json').read_text())
    html=(ROOT/'dashboard/index.html').read_text()
    global_js=(ROOT/'dashboard/global_ui.js').read_text()
    assert 'window.OrreryNetworkNode.render(name,g,' in html
    assert 'window.OrreryNetworkNode.render(a.name,' in global_js
