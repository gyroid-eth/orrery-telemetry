"""Bot-only TUI admission; ordinary child/CLI configuration remains unchanged."""
import json
from pathlib import Path

import pytest

import test_hermes_thin_config as thin
from test_hermes_tui_product import add_source_inputs, add_tools, make_product, details, pins, save, tui


@pytest.fixture
def bot():
    case = thin.ThinConfig('test_static_admission_does_not_create_profile_or_state')
    case.setUp()
    try:
        add_source_inputs(case.source)
        save(case.source / 'pm/lock.json', {'packages': pins()})
        add_tools(case.runtime, case.source)
        make_product(case.runtime)
        receipt = case.runtime / '.orrery-managed-install.json'
        runtime = {**case.template_data, **details(case.runtime), 'generation_receipt': str(receipt)}
        save(receipt, {'harness': 'hermes', 'hermes_commit': tui.BASE, 'complete': True, 'details': runtime})
        case.template_data['generation_receipt'] = str(receipt)
        case.save(case.template, case.template_data)
        client = case.base / 'client';client.mkdir();save(client / 'context.json', {})
        yield case, {'fresh_client_dir': str(client), 'telegram': {
            'token_ref': {'store': 'keychain', 'service': 'fixture.telegram', 'account': 'fixture'},
            'allow_from': [123]}}
    finally:case.doCleanups()


def test_bot_argv_and_only_verified_product_env_are_propagated(bot):
    case, telegram = bot
    # Path-shaped provider/model input never supplies a runtime override.
    case.profile_spec['HERMES_TUI_DIR'] = '/untrusted/product'
    case.profile_spec['HERMES_NODE'] = '/untrusted/node'
    case.save(case.template, case.template_data)
    plan = case.generate(**telegram)
    assert '--tui' in plan['argv']
    assert plan['env']['HERMES_TUI_DIR'] == str(case.runtime / 'tui-product')
    assert plan['env']['HERMES_NODE'] == str(case.runtime / 'tools/node-fixture/bin/node')
    assert plan['argv'][0] == case.template_data['hermes_executable']
    assert not any('npm' in argument for argument in plan['argv'])
    assert not (case.runtime / 'source/node_modules').exists()
    overlay = json.loads((Path(plan['config_dir']) / 'config.yaml').read_text())
    assert overlay['plugins']['entries'] == {'orrery-telegram': {'allow_gateway_injection': True}}


@pytest.mark.parametrize('damage', ['missing-entry', 'modified-entry', 'missing-receipt', 'selection', 'node'])
def test_bot_refuses_incomplete_tui_before_writing_profile_or_state(bot, damage):
    case, telegram = bot
    if damage == 'missing-entry':(case.runtime / 'tui-product/dist/entry.js').unlink()
    elif damage == 'modified-entry':(case.runtime / 'tui-product/dist/entry.js').write_text('changed')
    elif damage == 'missing-receipt':(case.runtime / '.orrery-managed-install.json').unlink()
    elif damage == 'node':(case.runtime / 'tools/node-fixture/bin/node').chmod(0o600)
    else:
        case.template_data['generation_receipt'] = str(case.base / 'other-receipt.json')
        case.save(case.template, case.template_data)
    with pytest.raises(ValueError, match='invalid_runtime'):case.generate(**telegram)
    assert not (Path(telegram['fresh_client_dir']) / 'providers').exists()
    assert not (case.base / 'child').exists()


def test_ordinary_children_do_not_require_or_inherit_tui_env(bot):
    case, _ = bot
    (case.runtime / '.orrery-managed-install.json').unlink()
    (case.runtime / 'tui-product/dist/entry.js').unlink()
    plan = case.generate()
    assert '--tui' not in plan['argv']
    assert 'HERMES_TUI_DIR' not in plan['env']
    assert 'HERMES_NODE' not in plan['env']
    assert thin.config.validate_input(case.document)['executable'] == plan['argv'][0]
    overlay = json.loads((Path(plan['config_dir']) / 'config.yaml').read_text())
    assert 'entries' not in overlay['plugins']
