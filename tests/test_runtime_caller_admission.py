"""Literal full caller admission; no provider/native capability simulation."""
from copy import deepcopy
import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def admission(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'bin/lib'))
    return importlib.import_module('runtime_local_launch')


def pair():
    caller = {'provider':'codex','model':'local:fixture','cwd':'/fixture/cwd',
        'task':'/fixture/task.md','name':'FixtureChild','parent':'FixtureParent',
        'base':'mail-only','tools':{},'clear_tools':False,'detached':True,
        'resources':['/fixture/Scratch.md'],'worktree':False,'chrome':False}
    # A comparison input only, not an admissible or resolved product intent.
    intent = {**deepcopy(caller), 'model':{'kind':'local-profile','ref':'local:fixture'}}
    return caller, intent


@pytest.mark.parametrize('field,value', [
    (None,None),('provider','claude'),('model','local:other'),('cwd','/other/cwd'),
    ('task','/other/task.md'),('name','OtherChild'),('parent','OtherParent'),
    ('base','default'),('tools',{'mcp':['fixture_extra']}),('clear_tools',True),
    ('detached',False),('resources',['/other/Scratch.md']),('worktree',True),('chrome',True),
    ('effort',''),('strict_output_cap',False),('task_delivery','mail'),
])
def test_full_caller_compares_every_literal_field_and_optional_presence(admission,field,value):
    caller, intent = pair()
    before = deepcopy((caller,intent))
    if field:
        caller[field] = value
        with pytest.raises(admission.LocalModelError,match='^LOCAL_MODEL_INTENT_CHANGED$'):
            admission.check_caller_intent(caller,intent)
    else:
        assert admission.check_caller_intent(caller,intent) == caller
        assert (caller,intent) == before


@pytest.mark.parametrize('field,value', [('root','/other'),('owner','/other/profile.json'),
                                        ('auth',{}),('effective_scope',{})])
def test_full_caller_never_accepts_operator_configuration(admission,field,value):
    caller, intent = pair()
    caller[field] = value
    with pytest.raises(admission.LocalModelError,match='^LOCAL_MODEL_CALLER_INVALID$'):
        admission.check_caller_intent(caller,intent)


@pytest.mark.parametrize('invalid',['mail','shell'])
def test_full_caller_uses_actual_existing_tools_grammar(admission,invalid):
    caller, intent = pair()
    caller['tools'] = {invalid:True}
    with pytest.raises(ValueError,match='unknown tools'):
        admission.check_caller_intent(caller,intent)


@pytest.mark.parametrize('field', ['agent_name','program','model','cwd'])
@pytest.mark.parametrize('value', [None,'explicit'])
def test_operator_rejects_mixed_full_and_minimal_fields_before_effects(admission,field,value):
    module = importlib.import_module('runtime_operator')
    operator = module.RuntimeOperator.__new__(module.RuntimeOperator)
    operator.api = {'ClientError':RuntimeError}
    effects = []
    def forbidden(*args,**kwargs):
        effects.append(True)
        raise AssertionError('mixed input reached an effect')
    for name in ('client','create','read','status','prepare'):
        setattr(operator,name,forbidden)
    caller, _ = pair()
    with pytest.raises(RuntimeError,match='^LOCAL_MODEL_CALLER_INVALID$'):
        operator.spawn('child_01',caller=caller,parent_client_name='parent',**{field:value})
    assert effects == []


@pytest.mark.parametrize('bad', [None,[],{'unknown':True},'not-an-object'])
def test_operator_rejects_bad_full_schema_before_effects(admission,bad):
    module = importlib.import_module('runtime_operator')
    operator = module.RuntimeOperator.__new__(module.RuntimeOperator)
    operator.api = {'ClientError':RuntimeError}
    effects = []
    def forbidden(*args,**kwargs):
        effects.append(True)
        raise AssertionError('invalid caller reached an effect')
    for name in ('client','create','read','status','prepare'):
        setattr(operator,name,forbidden)
    with pytest.raises(RuntimeError,match='^LOCAL_MODEL_CALLER_INVALID$'):
        operator.spawn('child_01',caller=bad,parent_client_name='parent')
    assert effects == []


@pytest.mark.parametrize('case',['matching','changed','missing-local','legacy-no-pin'])
def test_R2_digest_pin_is_checked_under_C_before_launch_effects(admission,tmp_path,case):
    # Unit predicate probe only. The local projection is a fixed input, not a
    # fake permission engine or native acceptance adapter. Stop before effects.
    module = importlib.import_module('runtime_delivery')
    held = []
    reached = []
    class Accepted(Exception):pass
    @contextmanager
    def mutex():
        held.append(True)
        try:yield
        finally:held.pop()
    def capabilities():
        assert held == [True]
        reached.append(True)
        raise Accepted()
    client = SimpleNamespace(operation_mutex=mutex,outputs={
        'launch':tmp_path/'absent-launch.json','task':tmp_path/'absent-task.md'},
        capabilities=capabilities)
    delivery = module.RuntimeDelivery(client,{'ClientError':RuntimeError})
    def projection(*args,**kwargs):
        assert held == [True]
        return None if case=='missing-local' else {
            'intent':{'intent_digest':'approved','task_sha256':None}}
    delivery.local_launch = projection
    kwargs = {} if case=='legacy-no-pin' else {
        'expected_intent_digest':'other' if case=='changed' else 'approved'}
    if case in ('matching','legacy-no-pin'):
        with pytest.raises(Accepted):delivery.prepare_launch('codex',cwd=tmp_path,**kwargs)
        assert reached == [True]
    else:
        with pytest.raises(RuntimeError,match='^LOCAL_MODEL_INTENT_CHANGED$'):
            delivery.prepare_launch('codex',cwd=tmp_path,**kwargs)
        assert reached == []
    assert not (tmp_path/'absent-launch.json').exists()


@pytest.mark.parametrize('pin',[None,'approved'])
def test_R2_start_forwards_only_an_explicit_digest_pin(admission,tmp_path,pin):
    # Unit argument handoff, deliberately stopped before launch preparation.
    module = importlib.import_module('runtime_delivery')
    class Forwarded(Exception):pass
    client = SimpleNamespace(outputs={'launch':tmp_path/'absent-launch.json'},
                             identity={'name':'FixtureChild'})
    delivery = module.RuntimeDelivery(client,{'ClientError':RuntimeError})
    delivery.local_launch = lambda *args,**kwargs:None
    delivery.codex_trust = lambda:None
    observed = []
    def prepare(provider,**kwargs):
        observed.append((provider,kwargs))
        raise Forwarded()
    delivery.prepare_launch = prepare
    kwargs = {} if pin is None else {'expected_intent_digest':pin}
    with pytest.raises(Forwarded):delivery.start('codex',tmp_path,**kwargs)
    expected = {'launch_kind':'startup','cwd':tmp_path}
    if pin is not None:expected['expected_intent_digest']=pin
    assert observed == [('codex',expected)]
