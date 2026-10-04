"""The dashboard's help map (dashboard/help_map.js).

Node cases need no browser. The page cases connect to an existing CDP browser
when ORRERY_TEST_CDP_URL is set (they never launch one) and drive the demo
mode of the real page, served from this checkout.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import random
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
HELP_MAP = DASHBOARD / "help_map.js"
NODE = shutil.which("node")


def node(script: str):
    if not NODE:
        pytest.skip("node not installed")
    result = subprocess.run([NODE, "-e", f"const H=require({json.dumps(str(HELP_MAP))});" + script],
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_page_server_and_demo_bundle_carry_the_help_map():
    index = (DASHBOARD / "index.html").read_text()
    assert '<link rel="stylesheet" href="help_map.css">' in index
    assert '<script src="help_map.js" defer></script>' in index
    server = (DASHBOARD / "server.py").read_text()
    assert '"/help_map.js"' in server and '"/help_map.css"' in server
    assert '"$DASH/help_map.js" "$DASH/help_map.css"' in (DASHBOARD / "demo" / "build.sh").read_text()


def test_usage_note_says_what_the_cockpit_help_map_says():
    # The cockpit (gyroid-eth/orrery bridge/cockpit_tour.js) annotates the same LEFT.
    notes = node("console.log(JSON.stringify(H.NOTES.map(n=>[n.id,n.copy])))")
    assert dict(notes)["usage"] == ("LEFT shows how much account allowance remains for Claude and Codex. "
                                    "Open it to see each window and when it resets.")
    assert len({note_id for note_id, _ in notes}) == len(notes)


def test_leader_geometry_tells_crossings_from_near_misses():
    result = node("""const g=H.geometry;console.log(JSON.stringify({
      cross:g.segmentsMeet([0,10,100,10],[50,0,50,40]),
      apart:g.segmentsMeet([0,10,40,10],[50,0,50,40]),
      along:g.segmentsMeet([0,10,100,10],[60,12,140,12]),
      parallel:g.segmentsMeet([0,10,100,10],[0,30,100,30]),
      through:g.segmentHitsRect([0,10,100,10],{l:40,t:0,r:60,b:20}),
      past:g.segmentHitsRect([0,30,100,30],{l:40,t:0,r:60,b:20})}))""")
    assert result == {"cross": True, "apart": False, "along": True, "parallel": False,
                      "through": True, "past": False}


def test_every_candidate_leader_leaves_its_control_and_ends_at_the_dot():
    result = node("""const r={l:100,t:100,r:200,b:130},out=[];
      for(const c of H.geometry.candidates(r,220,60,10,1200,800)){
        const [x0,y0]=c.points[0],[x1,y1]=c.points[c.points.length-1];
        const onEdge=(Math.abs(y0-r.b-3)<.5||Math.abs(y0-r.t+3)<.5)?x0>=r.l&&x0<=r.r:(y0>=r.t&&y0<=r.b&&(Math.abs(x0-r.r-3)<.5||Math.abs(x0-r.l+3)<.5));
        const square=c.points.every((p,i)=>!i||p[0]===c.points[i-1][0]||p[1]===c.points[i-1][1]);
        out.push(onEdge&&square&&x1===c.dot[0]&&y1===c.dot[1]&&(c.end?c.x+220===c.dot[0]-10:c.x===c.dot[0]+10));
        if(out.length>4000)break;
      }
      console.log(JSON.stringify({count:out.length,good:out.every(Boolean)}))""")
    assert result["count"] > 1000 and result["good"] is True


class _WebSocket:
    def __init__(self, url: str) -> None:
        _, rest = url.split("://", 1)
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.socket = socket.create_connection((host, int(port)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.socket.sendall(
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.socket.recv(1)
        self.buffer = response.split(b"\r\n\r\n", 1)[1]
        self.request_id = 0

    def _receive(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.socket.recv(65536)
            if not chunk:
                raise EOFError("CDP websocket closed")
            self.buffer += chunk
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result

    def call(self, method: str, **params: object) -> dict:
        self.request_id += 1
        data = json.dumps({"id": self.request_id, "method": method, "params": params}).encode()
        header = b"\x81" + (struct.pack("!B", len(data) | 0x80) if len(data) < 126
                            else struct.pack("!BH", 126 | 0x80, len(data)))
        mask = struct.pack("!I", random.getrandbits(32))
        self.socket.sendall(header + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(data)))
        while True:
            _first, second = self._receive(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._receive(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._receive(8))[0]
            message = json.loads(self._receive(size))
            if message.get("id") == self.request_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.socket.close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def demo_page(tmp_path):
    endpoint = os.environ.get("ORRERY_TEST_CDP_URL")
    if not endpoint:
        pytest.skip("set ORRERY_TEST_CDP_URL to an existing CDP browser")
    # The demo bundle, as published: build.sh is what adds the stories (and the help map).
    bundle = tmp_path / "demo"
    subprocess.run(["bash", str(DASHBOARD / "demo" / "build.sh"), str(bundle)], check=True,
                   capture_output=True, timeout=60)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(bundle)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    request = urllib.request.Request(endpoint + "/json/new?about:blank", method="PUT")
    tab = json.load(urllib.request.urlopen(request, timeout=5))
    client = _WebSocket(tab["webSocketDebuggerUrl"])

    def evaluate(expression: str):
        result = client.call("Runtime.evaluate", expression=expression, awaitPromise=True,
                             returnByValue=True, userGesture=True)
        assert "exceptionDetails" not in result, result
        return result.get("result", {}).get("value")

    def size(width: int, height: int) -> None:
        client.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                    deviceScaleFactor=1, mobile=False)

    def open_page(query: str = "") -> None:
        client.call("Page.navigate", url=f"http://127.0.0.1:{server.server_port}/index.html?{query}")
        for _ in range(100):
            if evaluate("Boolean(window.TelemetryHelpMap&&TelemetryHelpMap.show&&document.readyState==='complete')"):
                break
            time.sleep(.1)
        else:
            pytest.fail("help map did not mount")
        # Start the demo story and let the deck fill.
        evaluate("""(()=>{const go=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='START WATCHING');
          if(go)go.click();const s=document.createElement('style');s.textContent='#demo-strip{display:none!important}';
          document.head.appendChild(s);})()""")
        for _ in range(150):
            if evaluate("document.querySelectorAll('.bay .exitbtn').length>0"):
                break
            time.sleep(.1)

    try:
        client.call("Page.enable")
        client.call("Runtime.enable")
        size(1440, 900)
        yield client, evaluate, size, open_page
    finally:
        client.close()
        with contextlib.suppress(OSError):
            urllib.request.urlopen(endpoint + "/json/close/" + tab["id"], timeout=5)
        server.shutdown()
        server.server_close()


# The demo story keeps moving (cards arrive, the network settles), and the map
# follows on its next check; measure once the controls hold still for a moment.
LAYOUT = """new Promise(resolve=>{
  TelemetryHelpMap.show();let tries=0;
  const measure=()=>{TelemetryHelpMap.place();requestAnimationFrame(()=>requestAnimationFrame(()=>{
    const map=document.querySelector('.help-map');
    const notes=[...document.querySelectorAll('.help-map-note:not([hidden])')];
    const rect=el=>{const r=el.getBoundingClientRect();return {l:r.left,t:r.top,r:r.right,b:r.bottom};};
    const boxes=[...notes,document.querySelector('.help-map-title')].map(rect);
    const hit=(a,b)=>a.l<b.r&&a.r>b.l&&a.t<b.b&&a.b>b.t;
    const targets=notes.map(n=>TelemetryHelpMap.targetRect(TelemetryHelpMap.NOTES.find(x=>x.id===n.dataset.note)));
    const segments=[...document.querySelectorAll('.help-map-leader')].map(path=>{
      const pts=path.getAttribute('d').slice(1).split('L').map(p=>p.split(',').map(Number));
      return {id:path.dataset.note,pts,segs:pts.slice(1).map((p,i)=>[...pts[i],...p])};});
    const g=TelemetryHelpMap.geometry,crossings=[];
    segments.forEach((a,i)=>segments.slice(i+1).forEach(b=>{if(a.segs.some(s=>b.segs.some(t=>g.segmentsMeet(s,t))))crossings.push([a.id,b.id]);}));
    const leaders=segments.map(s=>{const i=notes.findIndex(n=>n.dataset.note===s.id),t=targets[i],n=boxes[i];
      const [x0,y0]=s.pts[0],[x1,y1]=s.pts[s.pts.length-1];
      return {id:s.id,fromTarget:x0>=t.l-6&&x0<=t.r+6&&y0>=t.t-6&&y0<=t.b+6,
        toNote:y1>=n.t&&y1<=n.b&&Math.min(Math.abs(x1-n.l),Math.abs(x1-n.r))<=14};});
    if(leaders.some(l=>!l.fromTarget)&&++tries<10){setTimeout(measure,300);return;}
    resolve({compact:map.classList.contains('compact'),fallback:map.dataset.fallback||null,
      notes:notes.map(n=>n.dataset.note),leaders,crossings,
      overlap:boxes.some((a,i)=>boxes.slice(i+1).some(b=>hit(a,b))),
      onTarget:boxes.some(b=>targets.some(t=>t&&hit(b,t))),
      within:boxes.every(b=>b.l>=0&&b.r<=innerWidth&&b.t>=0&&b.b<=innerHeight)});
  }));};
  measure();
})"""


def _assert_annotated(result: dict, expected: list[str]) -> None:
    assert result["compact"] is False, result
    assert result["notes"] == expected
    assert sorted(leader["id"] for leader in result["leaders"]) == sorted(expected)
    assert all(leader["fromTarget"] and leader["toNote"] for leader in result["leaders"]), result["leaders"]
    assert result["crossings"] == [], result["crossings"]
    assert (result["overlap"], result["onTarget"], result["within"]) == (False, False, True), result


@pytest.mark.parametrize("width,height", [(1920, 1080), (1440, 900), (1100, 760)])
def test_deck_controls_are_annotated_without_crossing(demo_page, width, height):
    _client, evaluate, size, open_page = demo_page
    size(width, height)
    open_page()
    _assert_annotated(evaluate(LAYOUT), ["view", "crew", "usage", "filter", "history", "new", "card", "exit"])


def test_network_controls_are_annotated_without_crossing(demo_page):
    _client, evaluate, size, open_page = demo_page
    open_page()
    evaluate("setView('net')")
    # Wait until the story has sent mail (a link to annotate), then let the layout settle.
    for _ in range(200):
        if evaluate("[...document.querySelectorAll('#net .edge-count')].some(e=>e.getBoundingClientRect().width>0)"):
            break
        time.sleep(.1)
    time.sleep(3)
    _assert_annotated(evaluate(LAYOUT), ["view", "crew", "usage", "window", "select", "settings", "agent", "link"])


def test_agent_panel_is_annotated_alone(demo_page):
    _client, evaluate, size, open_page = demo_page
    open_page()
    evaluate("openPanel(document.querySelector('.bay').dataset.name)")
    time.sleep(1)
    _assert_annotated(evaluate(LAYOUT), ["tabs", "actions", "role"])


def test_embedded_page_annotates_what_its_header_keeps(demo_page):
    _client, evaluate, size, open_page = demo_page
    size(1100, 760)
    open_page("embed=1")
    _assert_annotated(evaluate(LAYOUT), ["view", "filter", "history", "new", "card", "exit"])


def test_phone_width_uses_the_legend_and_every_way_out_closes(demo_page):
    _client, evaluate, size, open_page = demo_page
    size(420, 800)
    open_page()
    result = evaluate(LAYOUT)
    assert (result["compact"], result["leaders"], result["overlap"]) == (True, [], False)
    closes = evaluate("""(()=>{
      const map=document.querySelector('.help-map'),button=document.getElementById('helpmap-btn');
      document.querySelector('.help-map-note:not([hidden])').click();
      const legendKeeps=!map.hidden;
      document.querySelector('.help-map-close').click();
      const closeButton=map.hidden&&document.activeElement===button;
      button.click();const reopened=!map.hidden;
      document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
      const escape=map.hidden;
      button.click();map.dispatchEvent(new MouseEvent('click',{bubbles:true}));
      return {legendKeeps,closeButton,reopened,escape,anywhere:map.hidden,pressed:button.getAttribute('aria-pressed')};
    })()""")
    assert closes == {"legendKeeps": True, "closeButton": True, "reopened": True, "escape": True,
                      "anywhere": True, "pressed": "false"}
