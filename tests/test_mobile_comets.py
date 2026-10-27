"""Mobile Mail activity uses metadata and the existing explicit reading flow."""
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def node(code):
    prefix="""
const {Counts,MailComets}=require('./dashboard/mobile/mail_comets.js');
const snapshot=(count=12,extra={})=>({window:'day',sources:{mail:{status:'available'}},
 timeline:{scope:'fixture',edges_truncated:false},graph:{edges:[{source:'A',target:'B',kind:'to',count}]},...extra});
"""
    result=subprocess.run(['node','-e',prefix+code],cwd=ROOT,capture_output=True,
                          text=True,check=True,timeout=10)
    return json.loads(result.stdout)


def test_initial_snapshot_duplicate_direction_and_count_delta():
    got=node("""
const c=new Counts(),first=c.observe(snapshot()),delta=c.observe(snapshot(14)),duplicate=c.observe(snapshot(14));
const next=snapshot(15);next.graph.edges.push({source:'B',target:'A',kind:'cc',count:3});
console.log(JSON.stringify({first,delta,duplicate,next:c.observe(next),down:c.observe(snapshot(10)),afterDown:c.observe(snapshot(11))}));
""")
    assert got=={'first':[],'delta':[{'source':'A','target':'B','delta':2}],
                 'duplicate':[],'next':[{'source':'A','target':'B','delta':1},
                 {'source':'B','target':'A','delta':3}],'down':[],
                 'afterDown':[{'source':'A','target':'B','delta':1}]}


def test_kind_rows_aggregate_without_replaying_history_after_context_changes():
    got=node("""
const c=new Counts();c.observe(snapshot());
const s=snapshot(13);s.graph.edges.push({source:'A',target:'B',kind:'cc',count:2});
const combined=c.observe(s),reset=[];
reset.push(c.observe(snapshot(90,{window:'all'})));
reset.push(c.observe(snapshot(91,{timeline:{scope:'another'}})));
reset.push(c.observe(snapshot(92,{sources:{mail:{status:'stale'}}})));
reset.push(c.observe(snapshot(93)));
reset.push(c.observe(snapshot(94),false));reset.push(c.observe(snapshot(95)));
reset.push(c.observe(snapshot(96,{timeline:{scope:'fixture',edges_truncated:true}})));
reset.push(c.observe(snapshot(97)));
console.log(JSON.stringify({combined,reset}));
""")
    assert got['combined']==[{'source':'A','target':'B','delta':3}]
    assert got['reset']==[[]]*8


DOM="""
class Element{
 constructor(){this.style={setProperty(){}};this.children=[];this.listeners={};this.classList={toggle(){}};}
 appendChild(x){this.children.push(x);return x;} setAttribute(){}
 addEventListener(k,fn){this.listeners[k]=fn;} remove(){this.removed=true;}
 replaceChildren(){this.children=[];}
}
let reduced=false,now=0,nextFrame=0,opened=[],fetches=[];
global.fetch=(...args)=>{fetches.push(args);throw Error('unexpected content request');};
global.document={hidden:false,createElement:()=>new Element(),getElementById:()=>({hidden:false})};
global.matchMedia=()=>({get matches(){return reduced;},addEventListener(){}});
global.performance={now:()=>now};global.requestAnimationFrame=()=>++nextFrame;global.cancelAnimationFrame=()=>{};
const network={net:new Element(),map:new Map([['A',{x:20,y:40}],['B',{x:220,y:140}]]),
 view:{vx:0,vy:0,k:1},onMailPair:(...args)=>opened.push(args)};
"""


def test_one_comet_per_change_expires_and_opens_pair_only_on_tap():
    got=node(DOM+"""
const m=new MailComets(network);m.update(snapshot());m.update(snapshot(14));m.update(snapshot(14));
m.frame(800);const count=m.items.length,button=m.items[0].button,position=button.style.left;
const before=opened.length;button.listeners.click();
m.update(snapshot(15));m.frame(4000);
console.log(JSON.stringify({count,position,before,opened,fetches,remaining:m.items.length}));
""")
    assert got['count']==1 and got['before']==0
    assert 20<float(got['position'][:-2])<220
    assert got['opened']==[['A','B']] and got['fetches']==[]
    assert got['remaining']==0


def test_reduced_motion_is_static_and_pause_resets_baseline():
    got=node(DOM+"""
reduced=true;const m=new MailComets(network);m.update(snapshot());m.update(snapshot(14));
m.frame(200);const x=m.items[0].button.style.left;m.frame(1500);
const same=x===m.items[0].button.style.left;
m.pause();m.update(snapshot(99));const resumed=m.items.length;
m.update(snapshot(100));m.update(snapshot(101,{window:'all'}));
console.log(JSON.stringify({same,x,resumed,changed:m.items.length,fetches}));
""")
    assert got=={'same':True,'x':'120px','resumed':0,'changed':0,'fetches':[]}


def test_active_comets_are_bounded_and_hidden_routes_are_skipped():
    got=node(DOM+"""
const m=new MailComets(network);m.update(snapshot());
for(let i=13;i<40;i++)m.update(snapshot(i));const limit=m.items.length;
network.map.delete('B');m.frame(100);const missing=m.items.length;
console.log(JSON.stringify({limit,missing}));
""")
    assert got=={'limit':12,'missing':0}


def test_comet_script_is_served_through_the_existing_owner_gate():
    import http.client
    import threading
    from dashboard import mobile_relay as relay
    server=relay.make_server(relay.Config('owner@example.test','https://viewer.example.ts.net:9443'),
                             store=object(),test_port=0)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    def get(path,owner=True):
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        try:
            headers={'Host':'viewer.example.ts.net:9443'}
            if owner:headers['Tailscale-User-Login']='owner@example.test'
            connection.request('GET',path,headers=headers)
            response=connection.getresponse();return response.status,response.read()
        finally:connection.close()
    try:
        assert get('/mail_comets.js',False)[0]==403
        assert get('/mail_comets.js')==(200,(ROOT/'dashboard/mobile/mail_comets.js').read_bytes())
        assert b'<script defer src="/mail_comets.js"></script>' in get('/')[1]
    finally:
        server.shutdown();server.server_close();worker.join(3)
        assert not worker.is_alive()
