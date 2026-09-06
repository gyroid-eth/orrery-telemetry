"""Native Windows reports its launch boundary without starting Bash."""
import json
from pathlib import Path
import subprocess
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import dashboard.server as server


def test_catalog_http_does_not_start_bash(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTSTACK_SCIENTISTS_JSON", raising=False)
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "absent.sqlite3"))
    def forbidden(*args, **kwargs):
        raise AssertionError("Windows catalog must not start a subprocess")

    monkeypatch.setattr(server.subprocess, "run", forbidden)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{httpd.server_port}/api/spawn-names", timeout=2
        ) as response:
            assert response.status == 200
            payload = json.load(response)
        assert payload["names"]
        assert payload["adjectives"]
        assert "Native Windows" in payload["unavailable"]
        assert "WSL2" in payload["unavailable"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_missing_catalog_http_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTSTACK_SCIENTISTS_JSON", str(tmp_path / "absent.json"))
    def forbidden(*args, **kwargs):
        raise AssertionError("must not fall back to Bash")
    monkeypatch.setattr(server.subprocess, "run", forbidden)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for path, status in [("/api/spawn-names", 503), ("/api/suggest-name?scientist=Curie", 409)]:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}{path}", timeout=2)
            except urllib.error.HTTPError as response:
                with response:
                    assert response.code == status
                    assert json.load(response)["error"]
            else:
                raise AssertionError("missing vocabulary must not return success")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_unavailable_catalog_disables_spawn_and_ignores_stale_response():
    html = (Path(__file__).resolve().parents[2] / "dashboard/index.html").read_text(encoding="utf-8")
    function = html.split("async function loadSpawnCatalog(seq){", 1)[1].split("\nfunction restoreSpawnAdvanced", 1)[0]
    button_function = html.split("function updateSpawnButton(){", 1)[1].split("\nfunction populateParentSelect", 1)[0]
    script = "async function loadSpawnCatalog(seq){" + function + "\nfunction updateSpawnButton(){" + button_function + r'''
const assert=require('node:assert/strict');
let spmLoadSeq=2,spmReady=true,spmBusy=false,spmSelectedName='',spmIdentityState='auto';
const elements={};
const SPM=id=>elements[id]||(elements[id]={});
let status='';
function setSpawnStat(message){status=message;}
const reason='Native Windows launch catalog / spawn is not supported. Use WSL2, the primary Windows path.';
async function fetch(){return {ok:true,json:async()=>({unavailable:reason})};}
(async()=>{
  await loadSpawnCatalog(1);
  assert.equal(spmReady,true);
  assert.equal(status,'');
  await loadSpawnCatalog(2);
  assert.equal(SPM('spm-spawn').disabled,true);
  assert.equal(status,reason);
  assert.equal(SPM('spm-agent-strip').textContent,'scientist roster unavailable');
  assert.equal(SPM('spm-models').textContent,'engine catalog unavailable');
})();
'''
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, timeout=10)
