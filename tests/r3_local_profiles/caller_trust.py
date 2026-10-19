"""Explicit TEST hook resources, never native discovery or trust approval.

Use the existing test_global_runtime_delivery fixture shape. The launch-side
product verifier remains real; no app-server or provider protocol is emulated.
"""


def prepare_codex_trust(delivery, *, fixture_mcp=False):
    client = delivery.client
    home = delivery.directory(client.runtime_dir/'provider-home/codex')
    plugin = delivery.directory(home/'plugins/cache/fixture')
    delivery.directory(plugin/'hooks')
    plan_path, config_path = home/'hook-plan.json', home/'config.toml'
    assert not plan_path.exists() and not plan_path.is_symlink()
    assert not config_path.exists() and not config_path.is_symlink()
    hooks = {'hooks':{'SessionStart':[]}}
    delivery.api['atomic_json'](plugin/'hooks/hooks.json', hooks)
    trust = delivery.plugin_helpers()[0]
    plan = {'schema_version':1, 'approved':True, 'enable_hooks':True,
        'shared_codex_home':str(home), 'cwd':str(client.isolation),
        'plugin_id':'agentstack-codex-app@agentstack-global', 'plugin_root':str(plugin),
        'payload_digest':trust['payload_digest'](plugin), 'declared_hooks':hooks,
        'hooks':[{'key':'fixture-key', 'currentHash':'sha256:fixture'}]}
    delivery.api['atomic_json'](plan_path, plan)
    config = ('[plugins."agentstack-codex-app@agentstack-global"]\nenabled = true\n'
              '[hooks.state."fixture-key"]\nenabled = true\ntrusted_hash = "sha256:fixture"\n')
    if fixture_mcp:
        config += '[mcp_servers.fixture_extra]\ncommand = "/usr/bin/true"\nargs = []\n'
    delivery.api['atomic_bytes'](config_path, config.encode())
    assert delivery.codex_trust() == plan
    return plan
