"""Private failure reasons; no Keychain, provider, Telegram or live services."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from test_hermes_telegram import plugin
import test_hermes_thin_config as thin_fixture
import test_hermes_tui_product as hermes_tui

config = thin_fixture.config

REF = dict(store='keychain', service='fixture.reference', account='fixture-owner')
CANARY = 'synthetic-private-canary'


@pytest.fixture
def host(monkeypatch, tmp_path):
    """Native API seam: bind the manager HOME and merge its managed overlay."""
    profile = tmp_path / 'profile';profile.mkdir()
    overlay = tmp_path / 'overlay';overlay.mkdir()
    (profile / 'config.yaml').write_text('{}')
    settings = dict(token_ref=REF, allow_from=[123])
    (overlay / 'config.yaml').write_text(json.dumps({'telegram': settings}))
    bound = []
    @contextmanager
    def scope(home):
        bound.append(Path(home))
        try:yield
        finally:bound.pop()
    def load():
        assert bound == [profile]
        raw = config.read(bound[-1] / 'config.yaml')
        managed = config.read(Path(os.environ['HERMES_MANAGED_DIR']) / 'config.yaml')
        return {**raw, **managed}
    package = ModuleType('hermes_cli');package.__path__ = []
    cfg = ModuleType('hermes_cli.config');cfg.load_config_readonly = load
    loader = ModuleType('hermes_cli.plugins_loader');loader._plugin_home_scope = scope
    for name, module in [('hermes_cli', package), (cfg.__name__, cfg), (loader.__name__, loader)]:
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(overlay))
    monkeypatch.setenv('HOME', str(tmp_path / 'owner-home'))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'other-ambient-profile'))
    monkeypatch.setattr(plugin, '_is_tui', lambda: True)
    monkeypatch.setattr(plugin.settings.sys, 'platform', 'darwin')
    for key in ('TELEGRAM_BOT_TOKEN', 'HERMES_TELEGRAM_BOT_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    return SimpleNamespace(_manager=SimpleNamespace(home_path=profile)), overlay, cfg


@pytest.mark.parametrize('failure,reason,code', [
    ('nonzero', 'security-nonzero', 44), ('timeout', 'security-timeout', None),
    ('unavailable', 'security-unavailable', None), ('empty', 'empty-or-invalid-value', None),
    ('control', 'empty-or-invalid-value', None), ('encoding', 'empty-or-invalid-value', None),
])
def test_security_failures_keep_public_status_and_only_private_fixed_reason(host, monkeypatch, caplog, failure, reason, code):
    ctx, _, _ = host
    def run(argv, **kwargs):
        assert argv == ['/usr/bin/security', 'find-generic-password', '-a', REF['account'], '-s', REF['service'], '-w']
        assert 'env' not in kwargs  # inherit this gateway's HOME; never rewrite it
        assert os.environ['HOME'].endswith('owner-home')
        assert CANARY not in repr(argv) + repr(kwargs) + repr(dict(os.environ))
        if failure == 'timeout':raise subprocess.TimeoutExpired(argv, 30, output=CANARY, stderr=CANARY)
        if failure == 'unavailable':raise OSError(CANARY)
        if failure == 'encoding':raise UnicodeDecodeError('utf8', b'\xff', 0, 1, CANARY)
        value = CANARY if failure == 'nonzero' else '' if failure == 'empty' else CANARY + '\x00'
        return subprocess.CompletedProcess(argv, code or 0, value, CANARY)
    monkeypatch.setattr(plugin.settings.subprocess, 'run', run)
    home = ctx._manager.home_path
    log = home / 'logs/telegram-credential-diagnostic.json';log.parent.mkdir()
    log.write_text(CANARY);log.chmod(0o644)
    channel = plugin.TelegramChannel(ctx);channel.start()
    public = json.loads((log.parent / 'telegram-status.json').read_text())
    record = json.loads(log.read_text())
    assert public['state'] == 'credential-unavailable' and 'reason' not in public
    assert record['reason'] == reason and record['pid'] == os.getpid()
    assert set(record) == {'reason', 'pid', 'updated_at'} | ({'returncode'} if code is not None else set())
    if code is not None:assert type(record['returncode']) is int and record['returncode'] == code
    assert log.stat().st_mode & 0o777 == 0o600
    assert channel.token == '' and channel._thread is None
    assert CANARY not in log.read_text() + json.dumps(public) + caplog.text
    assert REF['service'] not in log.read_text() and REF['account'] not in log.read_text()


@pytest.mark.parametrize('fault,reason', [('configuration', 'configuration-invalid'),
    ('exception', 'configuration-invalid'), ('reference', 'reference-invalid'), ('store', 'unsupported-store'),
    ('platform', 'unsupported-store')])
def test_configuration_and_reference_failure_do_not_read_keychain(host, monkeypatch, caplog, fault, reason):
    ctx, overlay, cfg = host
    document = {'telegram': dict(token_ref=dict(REF), allow_from=[123])}
    if fault == 'configuration':document['telegram']['allow_from'] = []
    if fault == 'exception':
        def broken():raise RuntimeError(CANARY)
        cfg.load_config_readonly = broken
    if fault == 'reference':document['telegram']['token_ref'].pop('account')
    if fault == 'store':document['telegram']['token_ref']['store'] = 'unsupported'
    if fault == 'platform':monkeypatch.setattr(plugin.settings.sys, 'platform', 'linux')
    (overlay / 'config.yaml').write_text(json.dumps(document))
    def forbidden(*args, **kwargs):raise AssertionError('credential helper must not run')
    monkeypatch.setattr(plugin.settings.subprocess, 'run', forbidden)
    channel = plugin.TelegramChannel(ctx);channel.start()
    record = json.loads((ctx._manager.home_path / 'logs/telegram-credential-diagnostic.json').read_text())
    assert record['reason'] == reason and 'returncode' not in record
    assert channel.token == '' and CANARY not in caplog.text


def test_real_generated_overlay_reaches_plugin_and_reader_with_gateway_home(host, monkeypatch):
    ctx, _, _ = host
    fixture = thin_fixture.ThinConfig();fixture.setUp()
    try:
        hermes_tui.add_source_inputs(fixture.source)
        hermes_tui.save(fixture.source / 'pm/lock.json', {'packages': hermes_tui.pins()})
        hermes_tui.add_tools(fixture.runtime, fixture.source)
        hermes_tui.make_product(fixture.runtime)
        receipt = fixture.runtime / '.orrery-managed-install.json'
        runtime = {**fixture.template_data, **hermes_tui.details(fixture.runtime),
                   'generation_receipt': str(receipt)}
        hermes_tui.save(receipt, {'harness': 'hermes', 'hermes_commit': hermes_tui.tui.BASE,
                                  'complete': True, 'details': runtime})
        fixture.template_data['generation_receipt'] = str(receipt)
        fixture.save(fixture.template, fixture.template_data)
        client = fixture.base / 'client';client.mkdir()
        (client / 'context.json').write_text('{}')
        selected = dict(token_ref=dict(REF), allow_from=[123, 456])
        plan = fixture.generate(fresh_client_dir=str(client), telegram=selected)
        profile = Path(plan['profile_home'])
        # The host fixture's native API seam reads this actual generator output,
        # with home scope binding checked rather than mocking _configuration.
        ctx._manager.home_path = profile
        import hermes_cli.config as native_config
        import hermes_cli.plugins_loader as native_loader
        @contextmanager
        def scope(home):
            assert home == profile
            yield
        def load():
            raw = config.read(profile / 'config.yaml')
            overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
            return {**raw, **overlay}
        monkeypatch.setattr(native_loader, '_plugin_home_scope', scope)
        monkeypatch.setattr(native_config, 'load_config_readonly', load)
        calls = []
        def read(argv, **kwargs):
            assert 'env' not in kwargs
            assert os.environ['HOME'].endswith('owner-home')
            assert CANARY not in repr(argv) + repr(kwargs) + repr(dict(os.environ))
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, CANARY + '\n', '')
        monkeypatch.setattr(plugin.settings.subprocess, 'run', read)
        channel = plugin.TelegramChannel(ctx)
        assert channel.allow == {123, 456} and channel.token == CANARY
        assert channel.credential_failure is None and len(calls) == 1
        assert calls[0][-3:] == ['-s', REF['service'], '-w']
        assert os.environ['HERMES_HOME'].endswith('other-ambient-profile')
        assert not (profile / 'logs/telegram-credential-diagnostic.json').exists()
        for path in fixture.base.rglob('*'):
            if path.is_file() and not path.is_symlink():assert CANARY.encode() not in path.read_bytes()
    finally:fixture.doCleanups()


@pytest.mark.parametrize('reason', ['credential_timeout', 'credential_unavailable', 'unsupported_credential_store', CANARY])
def test_secret_service_failure_is_fixed_without_exception_text(monkeypatch, reason):
    def fail(_):raise ValueError(reason)
    monkeypatch.setattr(plugin.settings, 'reader', lambda: {'validate_ref': lambda ref: ref, 'read_ref': fail})
    failures = []
    assert plugin.settings.read_token({'store': 'secret-service', 'attributes': {'application': 'fixture'}},
        on_failure=lambda code, rc: failures.append((code, rc))) == ''
    expected = {'credential_timeout': 'secret-service-timeout',
                'unsupported_credential_store': 'unsupported-store'}.get(reason, 'secret-service-unavailable')
    assert failures == [(expected, None)]


def test_diagnostic_refuses_free_text_and_does_not_follow_replaced_leaf(tmp_path):
    target = tmp_path / 'untouched';target.write_text(CANARY)
    home = tmp_path / 'profile';(home / 'logs').mkdir(parents=True)
    path = home / 'logs/telegram-credential-diagnostic.json';path.symlink_to(target)
    with pytest.raises(ValueError, match='invalid_telegram_diagnostic'):
        plugin.managed_guard.write_credential_diagnostic(home, CANARY)
    plugin.managed_guard.write_credential_diagnostic(home, 'reference-invalid')
    assert target.read_text() == CANARY and not path.is_symlink()
    assert path.stat().st_mode & 0o777 == 0o600


def test_diagnostic_io_error_does_not_change_public_failure(host, monkeypatch, caplog):
    ctx, _, cfg = host
    cfg.load_config_readonly = lambda: {}
    def fail(*args):raise OSError(CANARY)
    monkeypatch.setattr(plugin.managed_guard, 'write_credential_diagnostic', fail)
    channel = plugin.TelegramChannel(ctx);channel.start()
    assert json.loads((ctx._manager.home_path / 'logs/telegram-status.json').read_text())['state'] == 'credential-unavailable'
    assert CANARY not in caplog.text
