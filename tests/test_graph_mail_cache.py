"""NETWORK の線の数が comet と同時に増える: 新しい message で graph の cache を作り直す。"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import dashboard.server as server

# server は dashboard/ を sys.path に足して `import graph_data` する。同じ module を直す。
if server.HERE not in sys.path:
    sys.path.insert(0, server.HERE)
import graph_data  # noqa: E402

INDEX = pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "index.html"


@pytest.fixture
def mail(tmp_path, monkeypatch):
    path = tmp_path / "mail.sqlite3"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT);
        CREATE TABLE message_recipients (message_id INTEGER NOT NULL,
          agent_id INTEGER NOT NULL, PRIMARY KEY(message_id, agent_id));
        INSERT INTO messages VALUES (1, 'a');
        INSERT INTO message_recipients VALUES (1, 1);
    """)
    con.commit()
    con.close()
    monkeypatch.setattr(graph_data, "DB_PATH", str(path))
    monkeypatch.setattr(server, "_GRAPH_CACHE", {"ts": 0, "data": None, "mail": None})
    builds = []

    def build():
        builds.append(time.time())
        return {"nodes": [], "edges": [], "spawn": [], "n": len(builds)}

    monkeypatch.setattr(graph_data, "build_graph", build)

    def write(sql, *args):
        c = sqlite3.connect(path)
        c.execute(sql, args)
        c.commit()
        c.close()

    return builds, write


def test_a_new_message_rebuilds_within_the_ttl(mail):
    builds, write = mail
    server._raw_graph()
    write("INSERT INTO messages VALUES (2, 'b')")
    write("INSERT INTO message_recipients VALUES (2, 1)")
    assert server._raw_graph()["n"] == 2


def test_without_new_mail_the_eight_second_cache_stays(mail):
    builds, _ = mail
    for _ in range(5):
        server._raw_graph()
    assert len(builds) == 1


def test_recipients_written_after_the_message_rebuild_again(mail):
    builds, write = mail
    write("INSERT INTO messages VALUES (2, 'b')")
    server._raw_graph()
    write("INSERT INTO message_recipients VALUES (2, 1)")
    server._raw_graph()
    assert len(builds) == 2


def test_a_message_arriving_during_the_build_rebuilds_on_the_next_request(mail, monkeypatch):
    builds, write = mail

    def build():
        builds.append(1)
        if len(builds) == 1:
            write("INSERT INTO messages VALUES (2, 'b')")
        return {"n": len(builds)}

    monkeypatch.setattr(graph_data, "build_graph", build)
    server._raw_graph()
    assert server._raw_graph()["n"] == 2
    assert server._raw_graph()["n"] == 2


def test_concurrent_requests_after_new_mail_build_once(mail, monkeypatch):
    builds, write = mail
    server._raw_graph()

    def slow():
        builds.append(1)
        time.sleep(0.2)
        return {"n": len(builds)}

    monkeypatch.setattr(graph_data, "build_graph", slow)
    write("INSERT INTO messages VALUES (2, 'b')")
    threads = [threading.Thread(target=server._raw_graph) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(builds) == 2


def test_an_unreadable_mail_db_keeps_the_time_cache(mail, monkeypatch, tmp_path):
    builds, _ = mail
    monkeypatch.setattr(graph_data, "DB_PATH", str(tmp_path / "missing.sqlite3"))
    server._raw_graph()
    server._raw_graph()
    assert len(builds) == 1
    assert graph_data.mail_mark() is None


def _function(html, name):
    m = re.search(r"(?:async )?function " + name + r"\(\)\{.*?\n\}", html, re.DOTALL)
    assert m, name
    return m.group(0)


def test_new_mail_refreshes_the_graph_once_and_coalesces():
    html = INDEX.read_text(encoding="utf-8")
    decl = re.search(r"let netMailRefresh=null,netMailRefreshAgain=false;", html)
    assert decl
    harness = r"""
let ticks=0,release=[];
async function netTick(){ticks++;await new Promise(r=>release.push(r));}
(async()=>{
  const a=netRefreshForMail();netRefreshForMail();netRefreshForMail();
  await new Promise(r=>setTimeout(r,0));
  const during=ticks;
  release.shift()();await new Promise(r=>setTimeout(r,0));
  release.shift()();await a;
  process.stdout.write(JSON.stringify({during,ticks}));
})();
"""
    out = subprocess.run(
        ["node", "-e", decl.group(0) + "\n" + _function(html, "netRefreshForMail") + harness],
        capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(out.stdout) == {"during": 1, "ticks": 2}

    pulse = r"""
let mailLastTs=1000,mailQueue=[],refreshes=0;
const mailSeenKeys=new Set(),mailSeenOrder=[];
const MAIL_SEEN_MAX=400,MAIL_QUEUE_MAX=40;
const document={hidden:false};const view='net',netSuspended=false;
function mailMessageKey(m){return String(m.id);}
function annotateBroadcastMessages(x){return x;}
function mailDrain(){}
function netRefreshForMail(){refreshes++;}
const replies=[[{id:1}],[{id:1}],[]];
async function fetch(){const messages=replies.shift();return {json:async()=>({ok:true,now:1,messages})};}
(async()=>{await mailPulseTick();await mailPulseTick();await mailPulseTick();
process.stdout.write(String(refreshes));})();
"""
    out = subprocess.run(["node", "-e", _function(html, "mailPulseTick") + pulse],
                         capture_output=True, text=True, timeout=10, check=True)
    assert out.stdout == "1"
