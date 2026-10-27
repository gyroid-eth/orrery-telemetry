"""Shared geometry/force behavior, independent of either page's adapters."""
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def node(code):
    result=subprocess.run(['node','-e',"const c=require('./dashboard/network_core.js');\n"+code],cwd=ROOT,capture_output=True,text=True,check=True,timeout=10)
    return json.loads(result.stdout)


def test_fixed_nodes_spring_damping_and_view_fit():
    got=node("""
const map=new Map([['A',{x:100,y:100,vx:0,vy:0,fixed:true}],['B',{x:320,y:100,vx:0,vy:0,fixed:false}]]);
const move=c.step(map,[{source:'A',target:'B',count:7}],[],{w:600,h:400},{L:100,KR:0,GR:0,KS:.1});
const fit=c.fit(new Map([['A',{x:100,y:100}],['B',{x:200,y:100}]]),{w:600,h:400});
console.log(JSON.stringify({a:map.get('A'),b:map.get('B'),move,fit,empty:c.fit(new Map(),{w:600,h:400})??null}));
""")
    assert got['a']=={'x':100,'y':100,'vx':0,'vy':0,'fixed':True}
    assert abs(got['b']['x']-309.68)<1e-10
    assert abs(got['move']-10.32)<1e-10
    assert got['fit']=={'k':2,'vx':0,'vy':0} and got['empty'] is None


def test_pair_repel_is_symmetric_and_cooling_reduces_motion():
    got=node("""
function run(cool){const m=new Map([['A',{x:100,y:100,vx:0,vy:0}],['B',{x:110,y:100,vx:0,vy:0}]]);c.step(m,[],[],{w:600,h:400},{L:100,KR:1000,GR:0,KS:0},cool);return [...m.values()];}
console.log(JSON.stringify({normal:run(1),cooled:run(.7)}));
""")
    assert abs(got['normal'][0]['x']-91.4)<1e-10
    assert abs(got['normal'][1]['x']-118.6)<1e-10
    assert got['cooled'][0]['x']>got['normal'][0]['x']


def test_directed_mail_counts_and_attention_foreground_are_shared():
    got=node("""
class El{
 constructor(t,a={}){this.tag=t;this.attrs=a;this.children=[];this.dataset={};}
 setAttribute(k,v){this.attrs[k]=String(v);}
 appendChild(e){if(e.parent)e.parent.children=e.parent.children.filter(x=>x!==e);this.children.push(e);e.parent=this;return e;}
 querySelectorAll(){return [];}
 addEventListener(){}
}
global.document={createElementNS:(_,t)=>new El(t)};
const svg=new El('svg'),map=new Map([['A',{state:'finished'}],['B',{state:'run',actState:'ask'}],['C',{state:'run',actState:'work'}],['D',{retired:true}]]);
const els=c.buildElements(svg,map,[{source:'A',target:'B',count:1},{source:'B',target:'A',count:7},{source:'A',target:'C',count:3}],[{source:'A',target:'B'},{source:'B',target:'C'}],{widthMultiplier:1,render:(n,g)=>new El('g',{name:n,role:g.role})});
console.log(JSON.stringify({widths:els.edge.slice(0,3).map(e=>e.ln.attrs['stroke-width']),arrows:els.edge.map(e=>e.ln.attrs['marker-end']||null),counts:els.badge.map(x=>x.tx.textContent),nodes:svg.children.filter(e=>e.tag==='g').map(e=>e.attrs)}));
""")
    assert got['widths']==['2.00','4.00','3.00']
    assert got['arrows']==['url(#mk-c)']*3+[None,None]
    assert got['counts']==[8,3]
    assert got['nodes']==[{'name':'A','role':'parent'},{'name':'D','role':'none'},
                          {'name':'C','role':'child'},{'name':'B','role':'both'}]
