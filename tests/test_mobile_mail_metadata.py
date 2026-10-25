"""Metadata-only Mail reads: real SQLite, mixed timestamps, no mailbox writes."""
from datetime import datetime, timezone
import sqlite3

import pytest

from dashboard import graph_data


@pytest.fixture
def mail():
    con = sqlite3.connect(':memory:')
    con.row_factory = sqlite3.Row
    con.executescript('''
        CREATE TABLE agents(id INTEGER PRIMARY KEY,project_id INTEGER,name TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY,project_id INTEGER,sender_id INTEGER,created_ts,
                              subject TEXT,body_md TEXT);
        CREATE TABLE message_recipients(message_id INTEGER,agent_id INTEGER,kind TEXT,read_ts,ack_ts,
                                       PRIMARY KEY(message_id,agent_id));
        INSERT INTO agents VALUES(1,1,'Alpha'),(2,1,'Beta'),(3,1,'Gamma'),(4,1,'Blind'),(5,2,'Other');
    ''')
    now = 1792915200
    stamps = [now * 1_000_000 - 60_000_000,
              datetime.fromtimestamp(now - 120, timezone.utc).isoformat(),
              (now - 7200) * 1_000_000,
              datetime.fromtimestamp(now - 172800, timezone.utc).isoformat(),
              'invalid-date']
    con.executemany('INSERT INTO messages VALUES(?,1,1,?,?,?)',
                    [(i + 1, stamp, 'NEVER-SUBJECT', 'NEVER-BODY') for i, stamp in enumerate(stamps)])
    con.executemany('INSERT INTO message_recipients VALUES(?,?,?,?,?)',
                    [(i, 2, 'to', None, None) for i in range(1, 6)] +
                    [(1, 3, 'cc', None, None), (1, 4, 'bcc', None, None),
                     (1, 5, 'to', None, None)])
    yield con, now
    con.close()


def test_recipient_row_counts_window_and_event_metadata(mail):
    con, now = mail
    traced = []
    con.set_trace_callback(traced.append)
    recent = graph_data.mobile_mail_metadata(con, 1, '15m', now=now)
    assert recent['status'] == 'available'
    assert {(e['source'], e['target'], e['kind'], e['count']) for e in recent['edges']} == {
        ('Alpha', 'Beta', 'to', 2), ('Alpha', 'Gamma', 'cc', 1)}
    assert len(recent['events']) == 3
    assert {e['event_at'] for e in recent['events']} == {now - 60, now - 120}
    assert all(set(e) == {'key', 'source', 'target', 'kind', 'count', 'event_at'} for e in recent['events'])
    assert len({e['key'] for e in recent['events']}) == 3
    assert recent['events'] == graph_data.mobile_mail_metadata(con, 1, '15m', now=now)['events']
    assert sum(e['count'] for e in graph_data.mobile_mail_metadata(con, 1, 'day', now=now)['edges']) == 4
    all_time = graph_data.mobile_mail_metadata(con, 1, 'all', now=now)
    assert sum(e['count'] for e in all_time['edges']) == 5
    assert all_time['invalid_timestamps'] == 1
    assert all_time['degraded'] is True
    assert not any('subject' in q.lower() or 'body_md' in q.lower() for q in traced)
    assert con.execute('SELECT count(*) FROM message_recipients WHERE read_ts IS NOT NULL OR ack_ts IS NOT NULL').fetchone()[0] == 0


def test_unknown_window_and_limits_are_explicit(mail):
    con, now = mail
    with pytest.raises(ValueError):
        graph_data.mobile_mail_metadata(con, 1, 'arbitrary', now=now)
    result = graph_data.mobile_mail_metadata(con, 1, 'all', now=now, event_limit=2)
    assert len(result['events']) == 2
    assert result['events_truncated'] is True
    assert sum(e['count'] for e in result['edges']) == 5


def test_real_dashboard_metadata_route_refuses_unavailable_and_global(mail,tmp_path,monkeypatch):
    import http.client
    import sys
    import threading
    from dashboard import server
    from _dashboard_http_fixture import dashboard_http_fixture
    con,_=mail;path=tmp_path/'fixture.sqlite3'
    con.commit()
    with sqlite3.connect(path) as copied:con.backup(copied)
    monkeypatch.setitem(sys.modules,'graph_data',graph_data)
    monkeypatch.setattr(graph_data,'PROJECT_HUMAN_KEY','')
    monkeypatch.setattr(graph_data,'PROJECT_ID',1)
    monkeypatch.setattr(server,'DB_PATH',str(path))
    monkeypatch.setattr(server,'GLOBAL_MODE',False)
    httpd=dashboard_http_fixture(server);worker=threading.Thread(target=httpd.serve_forever,daemon=True);worker.start()
    def get(query=''):
        c=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=3)
        try:
            c.request('GET','/api/mobile-mail'+query)
            r=c.getresponse();return r.status,r.read()
        finally:c.close()
    try:
        assert get('?window=all')[0]==200
        assert get('?window=all&window=day')[0]==400
        monkeypatch.setattr(server,'DB_PATH',str(tmp_path/'missing.sqlite3'))
        status,body=get();assert status==503 and b'unavailable' in body
        monkeypatch.setattr(server,'GLOBAL_MODE',True)
        monkeypatch.setattr(server,'_db',lambda:pytest.fail('must not read legacy DB in global mode'))
        # Existing global operator admission rejects an unconfigured fixture
        # before route dispatch. Do not weaken it for the mobile endpoint.
        assert get()[0]==503
        status,body=server.mobile_mail_payload('all')
        assert status==501 and body['error']=='mobile_global_unsupported'
    finally:
        httpd.shutdown();httpd.server_close();worker.join(3)
        assert not worker.is_alive()


def test_approved_synthetic_roster_period_counts_and_projection():
    import json
    from pathlib import Path
    from dashboard.mobile_projection import SnapshotStore
    fixture=json.loads((Path(__file__).parent/'fixtures/mobile_snapshot.json').read_text())
    con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row;now=1792915200
    try:
        con.executescript('CREATE TABLE agents(id INTEGER PRIMARY KEY,project_id INTEGER,name TEXT);CREATE TABLE messages(id INTEGER PRIMARY KEY,project_id INTEGER,sender_id INTEGER,created_ts);CREATE TABLE message_recipients(message_id INTEGER,agent_id INTEGER,kind TEXT,PRIMARY KEY(message_id,agent_id));')
        ids={a['name']:i+1 for i,a in enumerate(fixture['agents'])}
        con.executemany('INSERT INTO agents VALUES(?,1,?)',[(i,n) for n,i in ids.items()])
        for i,row in enumerate(fixture['recipient_rows'],1):
            con.execute('INSERT INTO messages VALUES(?,1,?,?)',(i,ids[row['source']],(now-row['age_seconds'])*1000000))
            con.execute('INSERT INTO message_recipients VALUES(?,?,?)',(i,ids[row['target']],row['kind']))
        def fetch(key):
            if key=='agents':return {'agents':fixture['agents']}
            if key=='graph':return {'nodes':fixture['agents'],'spawn':fixture['spawn']}
            return graph_data.mobile_mail_metadata(con,1,key[5:],now=now)
        store=SnapshotStore(fetch,clock=lambda:now)
        all_time=store.snapshot('all');recent=store.snapshot('15m');day=store.snapshot('day')
        assert len(all_time['agents'])==36 and len(all_time['graph']['edges'])==47
        assert len(all_time['graph']['spawn'])==5 and len(all_time['timeline']['mail'])==82
        assert [len(x['graph']['edges']) for x in (recent,day)]==[7,25]
        unknown=next(a for a in all_time['agents'] if a['name']=='BrightHopper')
        assert unknown['running'] is None and unknown['ctx_used'] is None
        assert all_time['timeline']['states']==[] and all_time['quota']['status']=='unavailable'
    finally:con.close()


def test_fine_period_sql_counts_and_inclusive_mixed_timestamp_boundary(mail):
    con,now=mail
    assert sum(e['count'] for e in graph_data.mobile_mail_metadata(con,1,'m60',now=now)['edges'])==3
    assert sum(e['count'] for e in graph_data.mobile_mail_metadata(con,1,'m180',now=now)['edges'])==4
    con.executemany('INSERT INTO messages VALUES(?,1,1,?,?,?)',[
        (10,(now-3600)*1000000,'SUBJECT','BODY'),
        (11,(now-3600)*1000000-1,'SUBJECT','BODY'),
        (12,datetime.fromtimestamp(now-3600,timezone.utc).isoformat(),'SUBJECT','BODY')])
    con.executemany('INSERT INTO message_recipients VALUES(?,2,\'to\',NULL,NULL)',[(10,),(11,),(12,)])
    result=graph_data.mobile_mail_metadata(con,1,'m60',now=now)
    assert sum(e['count'] for e in result['edges'])==5
    assert sum(e['event_at']==now-3600 for e in result['events'])==2
    for period in ('m0','m1','m31','m43215','m1.5','m-15','m00015'):
        with pytest.raises(ValueError):graph_data.mobile_mail_metadata(con,1,period,now=now)
