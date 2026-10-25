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


def test_detail_resolves_exact_delivery_before_bounded_content(mail):
    con,now=mail
    recent=graph_data.mobile_mail_metadata(con,1,'day',now=now)
    event=recent['events'][0]
    content=graph_data.mobile_mail_detail(con,1,'day',event['key'],now=now)
    assert content['event']==event and content['subject']=='NEVER-SUBJECT'
    assert content['body']=='NEVER-BODY'
    revision=graph_data.mobile_mail_detail(con,1,'day',event['key'],mode='revision',now=now)
    assert revision['revision']==content['revision'] and 'body' not in revision and 'subject' not in revision
    trace=[];con.set_trace_callback(trace.append)
    with pytest.raises(LookupError):
        graph_data.mobile_mail_detail(con,1,'day','f'*32,now=now)
    assert not any('body_md' in sql.lower() or 'subject' in sql.lower() for sql in trace)
    assert con.execute('SELECT count(*) FROM message_recipients WHERE read_ts IS NOT NULL OR ack_ts IS NOT NULL').fetchone()[0]==0


def test_mail_cursor_reaches_older_event_without_exposing_content(mail):
    import json
    con,now=mail;registry=graph_data.MobileMailCursors(clock=lambda:now)
    head=graph_data.mobile_mail_metadata(con,1,'all',now=now,event_limit=2,cursors=registry)
    assert head['next_cursor'] and len(head['scope'])==32
    page=graph_data.mobile_mail_page(con,1,'all',head['next_cursor'],now=now,cursors=registry)
    assert not {e['key'] for e in page['events']} & {e['key'] for e in head['events']}
    assert 'NEVER' not in json.dumps(page)
    assert page['next_cursor']
    last=graph_data.mobile_mail_page(con,1,'all',page['next_cursor'],now=now,cursors=registry)
    old=next(e for e in last['events'] if e['event_at']<now-86400)
    assert graph_data.mobile_mail_detail(con,1,'all',old['key'],now=now)['body']=='NEVER-BODY'


def test_exact_detail_rejects_bcc_project_deleted_or_changed_delivery_before_content(mail):
    import hashlib
    con,now=mail;trace=[];con.set_trace_callback(trace.append)
    key=lambda mid,rid:hashlib.sha256(f'1:{mid}:{rid}'.encode()).hexdigest()[:32]
    for pid,event in [(1,key(1,4)),(1,key(1,5)),(2,key(1,2))]:
        trace.clear()
        with pytest.raises(LookupError):graph_data.mobile_mail_detail(con,pid,'all',event,now=now)
        assert not any('body_md' in q.lower() or 'subject' in q.lower() for q in trace)
    con.execute("UPDATE message_recipients SET kind='bcc' WHERE message_id=1 AND agent_id=2")
    trace.clear()
    with pytest.raises(LookupError):graph_data.mobile_mail_detail(con,1,'all',key(1,2),now=now)
    assert not any('body_md' in q.lower() for q in trace)


def test_content_caps_literal_utf8_and_revision_changes(mail):
    con,now=mail;event=graph_data.mobile_mail_metadata(con,1,'day',now=now)['events'][0]
    hostile='<img src="https://invalid.test/" onerror="alert(1)">\n'
    con.execute('UPDATE messages SET subject=?,body_md=? WHERE id=1',('S'*513,hostile+'猫'*30000))
    result=graph_data.mobile_mail_detail(con,1,'day',event['key'],now=now)
    assert result['body'].startswith(hostile) and len(result['body'].encode())<=65536
    assert len(result['subject'])==512 and result['subject_truncated'] and result['body_truncated']
    con.execute("UPDATE messages SET body_md='Changed' WHERE id=1")
    changed=graph_data.mobile_mail_detail(con,1,'day',event['key'],now=now)
    assert changed['revision']!=result['revision']
    con.execute('DELETE FROM messages WHERE id=1')
    with pytest.raises(LookupError):graph_data.mobile_mail_detail(con,1,'day',event['key'],now=now)


def test_pair_filter_before_limit_and_cursor_context_expiry(mail):
    con,now=mail;clock=[0];registry=graph_data.MobileMailCursors(clock=lambda:clock[0])
    con.executemany('INSERT INTO messages VALUES(?,1,1,?,?,?)',[(i,(now-1)*1000000,'SECRET','BODY') for i in range(100,310)])
    con.executemany("INSERT INTO message_recipients VALUES(?,3,'to',NULL,NULL)",[(i,) for i in range(100,310)])
    page=graph_data.mobile_mail_page(con,1,'all',now=now,cursors=registry,pair=('Alpha','Beta'),direction='forward')
    assert len(page['events'])==4 and all(e['target']=='Beta' for e in page['events'])
    reverse=graph_data.mobile_mail_page(con,1,'all',now=now,pair=('Alpha','Beta'),direction='reverse')
    assert reverse['events']==[]
    head=graph_data.mobile_mail_metadata(con,1,'all',now=now,cursors=registry)
    token=head['next_cursor'];assert token
    for opts in ({'window':'day'},{'window':'all','pair':('Alpha','Beta')},{'window':'all','pid':2}):
        opts={'pid':1,**opts}
        with pytest.raises(LookupError):graph_data.mobile_mail_page(con,cursor=token,now=now,cursors=registry,**opts)
    page=graph_data.mobile_mail_page(con,1,'all',token,now=now,cursors=registry)
    assert not {e['key'] for e in page['events']} & {e['key'] for e in head['events']}
    clock[0]=600
    with pytest.raises(LookupError):graph_data.mobile_mail_page(con,1,'all',token,now=now,cursors=registry)
    assert len(registry._items)==0


def test_batch_distinct_2000_boundary_and_actual_json_escape_budget(mail):
    import json
    con,now=mail;con.execute('DELETE FROM message_recipients');con.execute('DELETE FROM messages')
    con.executemany('INSERT INTO messages VALUES(?,1,1,?,?,?)',[(i,(now-1)*1000000,'S','B') for i in range(1,2002)])
    con.executemany("INSERT INTO message_recipients VALUES(?,?,'to',NULL,NULL)",[(i,r) for i in range(1,2002) for r in (2,3)])
    registry=graph_data.MobileMailCursors();cursor=None;items=[];pages=0
    while True:
        page=graph_data.mobile_mail_cache_page(con,1,cursor,now=now,cursors=registry);pages+=1
        assert len(page['items'])<=32 and len(json.dumps(page,separators=(',',':')).encode())<4*1024*1024
        items.extend(page['items']);cursor=page['next_cursor']
        if not cursor:break
        assert pages<64
    assert pages==63 and len(items)==2000 and len({x['fingerprint'] for x in items})==2000
    assert page['complete'] is False and page['reason']=='count_limit'
    # Delete the oldest extra message: exact 2000 must be complete, recipients
    # never double body count. The final page has sixteen distinct messages.
    con.execute('DELETE FROM messages WHERE id=1');cursor=None;count=0
    while True:
        page=graph_data.mobile_mail_cache_page(con,1,cursor,now=now,cursors=registry);count+=len(page['items']);cursor=page['next_cursor']
        if not cursor:break
    assert count==2000 and page['complete'] is True and len(page['items'])==16
    con.execute('UPDATE messages SET body_md=?',('\x00'*65536,))
    escaped=graph_data.mobile_mail_cache_page(con,1,now=now)
    assert 0<len(escaped['items'])<32 and escaped['next_cursor'] and not escaped['complete']
    assert len(json.dumps(escaped,separators=(',',':')).encode())<4*1024*1024


def test_real_loopback_content_page_and_batch_routes_keep_global_fail_closed(mail,tmp_path,monkeypatch):
    import http.client,json,sys,threading
    from dashboard import server
    from _dashboard_http_fixture import dashboard_http_fixture
    con,now=mail;con.commit();path=tmp_path/'mail-fixture.sqlite3'
    with sqlite3.connect(path) as copied:con.backup(copied)
    monkeypatch.setitem(sys.modules,'graph_data',graph_data)
    monkeypatch.setattr(graph_data,'PROJECT_HUMAN_KEY','');monkeypatch.setattr(graph_data,'PROJECT_ID',1)
    monkeypatch.setattr(server,'DB_PATH',str(path));monkeypatch.setattr(server,'GLOBAL_MODE',False)
    event=graph_data.mobile_mail_metadata(con,1,'all',now=now)['events'][0]
    httpd=dashboard_http_fixture(server);worker=threading.Thread(target=httpd.serve_forever,daemon=True);worker.start()
    def get(route):
        c=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=3)
        try:
            c.request('GET',route);r=c.getresponse();return r.status,dict(r.getheaders()),json.loads(r.read())
        finally:c.close()
    try:
        status,headers,detail=get('/api/mobile-message?window=all&event='+event['key'])
        assert status==200 and detail['body']=='NEVER-BODY' and headers['Cache-Control']=='no-store'
        assert get('/api/mobile-message?event='+event['key']+'&mode=invalid')[0]==400
        assert get('/api/mobile-message?event='+'f'*32)[0]==410
        status,_,page=get('/api/mobile-mail-page?window=all&a=Alpha&b=Beta&direction=both')
        assert status==200 and all(e['target']=='Beta' for e in page['events']) and 'NEVER' not in json.dumps(page)
        status,_,batch=get('/api/mobile-cache-page');assert status==200 and len(batch['items'])<=32
        for route in ('message','page','cache'):
            monkeypatch.setattr(server,'GLOBAL_MODE',True)
            status,value=server.mobile_mail_content_payload(route,{})
            assert status==501 and value['error']=='mobile_global_unsupported'
        assert con.execute('SELECT count(*) FROM message_recipients WHERE read_ts IS NOT NULL OR ack_ts IS NOT NULL').fetchone()[0]==0
    finally:httpd.shutdown();httpd.server_close();worker.join(3);assert not worker.is_alive()
