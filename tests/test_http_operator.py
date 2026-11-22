"""Private operator capability and browser provenance share one admission."""
from email.message import Message
import json
import os
from pathlib import Path

import pytest
from dashboard.http_operator import Admission, AdmissionError, configure, read_capability


@pytest.fixture
def operator(tmp_path):
    home=tmp_path/'agentstack'
    path=configure(home)
    return home,path,Admission(home,'http://127.0.0.1:18641')


def headers(capability, **extra):
    h=Message()
    h['Host']='127.0.0.1:18641'
    h['Authorization']='Bearer '+capability
    for k,v in extra.items():h[k]=v
    return h


def test_explicit_configure_and_read_are_stable(operator):
    home,path,admit=operator
    before=path.read_bytes()
    token=read_capability(home)
    assert len(token)==43 and token not in str(path)
    assert path.stat().st_mode&0o777==0o600
    admit.http(headers(token),'127.0.0.1')
    admit.http(headers(token,Origin=admit.origin,Referer=admit.origin+'/network/',**{'Sec-Fetch-Site':'same-origin'}),'::1')
    with pytest.raises(FileExistsError):configure(home)
    assert path.read_bytes()==before


@pytest.mark.parametrize('field,value',[
    ('Host','attacker.example:18641'),('Host','127.0.0.1:18642'),
    ('Origin','null'),('Origin','http://localhost:18641'),
    ('Origin','https://attacker.example'),('Referer','http://attacker.example/page'),
    ('Sec-Fetch-Site','cross-site'),('Sec-Fetch-Site','same-site'),
    ('Authorization','Bearer wrong'),('Authorization',''),
])
def test_network_and_auth_reject_before_dispatch(operator,field,value):
    home,path,admit=operator
    h=headers(read_capability(home))
    h.replace_header(field,value) if field in h else h.add_header(field,value)
    reason = 'HTTP_HOST_FORBIDDEN' if field=='Host' else ('HTTP_OPERATOR_REQUIRED' if field=='Authorization' else 'HTTP_ORIGIN_FORBIDDEN')
    with pytest.raises(AdmissionError,match=reason):admit.http(h,'127.0.0.1')


@pytest.mark.parametrize('field',['Host','Origin','Referer','Sec-Fetch-Site','Authorization'])
def test_duplicate_headers_fail_closed(operator,field):
    home,path,admit=operator
    h=headers(read_capability(home));h[field]=h.get(field,'x')
    if len(h.get_all(field))==1:h[field]='x'
    with pytest.raises(AdmissionError):admit.http(h,'127.0.0.1')


def test_ws_upgrade_only_checks_network_then_first_frame_auth(operator):
    home,path,admit=operator
    h=Message();h['Host']='127.0.0.1:18641';h['Origin']=admit.origin
    admit.network(h,'127.0.0.1',websocket=True)
    admit.authenticate(read_capability(home))
    with pytest.raises(AdmissionError):admit.authenticate('\ud800')
    del h['Origin']
    with pytest.raises(AdmissionError):admit.network(h,'127.0.0.1',websocket=True)
    with pytest.raises(AdmissionError):admit.http(headers(read_capability(home)),'192.0.2.1')


@pytest.mark.parametrize('change',['mode','hardlink','symlink','wrong-kind','foreign-uid','missing'])
def test_private_file_admission_and_no_automatic_repair(operator,monkeypatch,change):
    home,path,admit=operator
    token=read_capability(home);original=path.read_bytes()
    if change=='mode':path.chmod(0o644)
    elif change=='hardlink':os.link(path,home/'other')
    elif change=='symlink':
        target=home/'original';path.rename(target);path.symlink_to(target)
    elif change=='wrong-kind':path.write_text(json.dumps({'kind':'other','capability':token}))
    elif change=='foreign-uid':monkeypatch.setattr(os,'getuid',lambda:path.stat().st_uid+1)
    else:path.unlink()
    before=path.read_bytes() if path.exists() else None
    with pytest.raises(AdmissionError,match='HTTP_OPERATOR_UNAVAILABLE'):admit.http(headers(token),'127.0.0.1')
    assert (path.read_bytes() if path.exists() else None)==before


@pytest.mark.parametrize('action',['configure','show'])
def test_operator_cli_requires_explicit_home(tmp_path,action):
    import os,subprocess,sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    home=tmp_path/'home';home.mkdir()
    env={k:v for k,v in os.environ.items() if k!='AGENTSTACK_HOME'}
    env['HOME']=str(home)
    result=subprocess.run([sys.executable,str(root/'bin/agentstack-http-operator'),action],
        env=env,capture_output=True,text=True,timeout=10)
    assert result.returncode==2 and 'HTTP_OPERATOR_HOME_REQUIRED' in result.stderr
    assert not (home/'.agentstack').exists()
