"""Closed status/resolve argv; fixed Store seam, fake owner/process queries."""
import importlib
import io
import json

import pytest

from test_runtime_legacy_registration import store,send,query_identity


def module():
    try:return importlib.import_module('runtime_legacy_preview_cli')
    except ModuleNotFoundError:pytest.fail('PREVIEW_STATUS_RESOLVE_NOT_IMPLEMENTED')


@pytest.mark.parametrize('args',[
    [],['status'],['status','../Other'],['status','FixtureCurie','--owner=/elsewhere'],
    ['resolve','FixtureCurie'],['resolve','FixtureCurie','--action=retry','--expected-state-digest='+'a'*64],
    ['resolve','OtherCurie','--action=abandon','--expected-state-digest='+'a'*64],
])
def test_unknown_argv_or_other_target_cannot_select_an_owner(store,args):
    output=io.StringIO();before=store.owner.read_bytes()
    assert module().main(args,_store=store,_stdout=output,_stderr=output)!=0
    assert store.owner.read_bytes()==before and not store.pending.exists()


def test_status_prints_only_checked_nonsecret_data_without_an_observation(store):
    m=module();output=io.StringIO();before=store.owner.read_bytes()
    assert m.main(['status','FixtureCurie'],_store=store,_stdout=output,
        _query_child=lambda token:pytest.fail('status authenticated'),
        _observe_start=lambda pid:pytest.fail('status observed'))==0
    assert json.loads(output.getvalue())==store.status()
    assert store.owner.read_bytes()==before


def test_closed_resolve_can_only_finalize_the_saved_registration(store):
    m=module();send(store);output=io.StringIO()
    assert m.main(['resolve','FixtureCurie','--action=finalize-registration',
        '--expected-state-digest='+store.status()['state_digest']],_store=store,
        _query_child=query_identity,_stdout=output)==0
    assert json.loads(output.getvalue())['phase']=='registered'
    assert 'registration_token' not in output.getvalue()


def test_closed_abandon_cancels_only_the_prepared_request_without_querying_mail(store):
    m=module();store.begin(candidate_token='fixture-candidate-credential-000000000000',
        expected_digest=store.status()['state_digest']);output=io.StringIO()
    assert m.main(['resolve','FixtureCurie','--action=abandon','--expected-state-digest='+store.status()['state_digest']],
        _store=store,_stdout=output,_query_child=lambda token:pytest.fail('abandon queried Mail'))==0
    assert json.loads(output.getvalue())['phase']=='cancelled'


def test_argument_errors_never_echo_caller_secret_values(store):
    output=io.StringIO()
    assert module().main(['status','FixtureCurie','--arbitrary-secret=DO_NOT_ECHO'],
        _store=store,_stdout=output,_stderr=output)!=0
    assert 'DO_NOT_ECHO' not in output.getvalue()
