"""TEST-only installed payload, copied by the installer's real declarations.

No installer main, services, user settings, enrollment or native CLI is run.
The configure manifest is confined to the fixture's private install directory.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def shell_function(text, name):
    match = re.search(r'^'+re.escape(name)+r'\(\) \{\n.*?^\}\n', text, re.M|re.S)
    assert match, 'installer declaration missing: '+name
    return match.group()


def _copied_sources(log, install):
    """Map actual cp declarations, including renames, back to source bytes."""
    result = {}
    for line in log.read_text().splitlines():
        args = json.loads(line)
        operands = [arg for arg in args if not arg.startswith('-')]
        assert len(operands)==2, 'unrecognized installer cp declaration'
        raw, target = operands
        source, destination = Path(raw), Path(target)
        if source.is_dir():
            base = destination if raw.endswith('/.') else destination/source.name
            for item in source.rglob('*'):
                if item.is_file() or item.is_symlink():
                    result[str((base/item.relative_to(source)).relative_to(install))]=item
        else:
            output = destination/source.name if destination.is_dir() else destination
            result[str(output.relative_to(install))]=source
    # install_child_mcp_proxy uses the installer's embedded shutil.copytree,
    # rather than cp. Its mirrored paths must independently match checkout
    # bytes too; no resolver or observed command supplies expected bytes.
    for item in install.rglob('*'):
        if item.is_file() or item.is_symlink():
            relative=str(item.relative_to(install))
            result.setdefault(relative,ROOT/relative)
    return result


def installer_manifest_inventory(install):
    """Execute the real manifest's payload inventory, not a second file list."""
    text=(ROOT/'scripts/install.sh').read_text()
    start=text.index('owned_files = []',text.index('write_manifest()'))
    end=text.index('services = []',start)
    inventory=text[start:end]
    replacements={
        'INSTALL_DIR':str(install),'ENV_FILE':str(install/'env.sh'),
        'MANIFEST':str(install/'install-state.json'),
        'SAFE_MERGE_RESULT_FILE':str(install.parent/'absent-settings-result'),
        'MCP_MERGE_RESULT_FILE':str(install.parent/'absent-mcp-result'),
        'NATIVE_MAIL_ENV':str(install.parent/'absent-native-env'),
        'NATIVE_MAIL_RUNNER':str(install.parent/'absent-native-runner'),
        'NATIVE_MAIL_DEPLOYMENT':str(install.parent/'absent-native-deployment')}
    for key,value in replacements.items():
        inventory=inventory.replace('$'+key,value)
    assert '$' not in inventory, 'installer inventory gained an unbound input'
    import pathlib
    namespace={'pathlib':pathlib,'json':json,'os':os,'install_dir':install,'skill_dirs':(),
        'service_path':'','mail_autostart_path':'','mail_autostart_service_path':'','mail_watcher_path':''}
    exec(compile(inventory,str(ROOT/'scripts/install.sh')+':manifest-inventory','exec'),namespace)
    return namespace['owned_files'],namespace['owned_dirs']


def copy_installer_payload(install):
    """Invoke only the exact copy declarations, under a new private root."""
    install=Path(install)
    assert install.is_absolute() and install.resolve()==install and not install.exists()
    install.mkdir(mode=0o700)
    for name in ('bin','hooks','dashboard','skills'):(install/name).mkdir(mode=0o700)
    text=(ROOT/'scripts/install.sh').read_text()
    declarations=text[text.index('RUNTIME_SCHEMA_SOURCE='):text.index('is_optional_provider_payload()')]
    setup={
        'REPO_ROOT':ROOT,'SCRIPT_DIR':ROOT/'scripts','INSTALL_DIR':install,
        'BIN_DIR':install/'bin','HOOKS_DIR':install/'hooks',
        'DASHBOARD_DIR':install/'dashboard','SKILLS_DIR':install/'skills',
        'PYTHON_BIN':sys.executable,'fixture_copy_log':install.parent/'installer-copy.jsonl'}
    command='set -euo pipefail\n'+''.join(key+'='+shlex.quote(str(value))+'\n' for key,value in setup.items())
    command+='\n'.join(line for line in text.splitlines() if line.startswith(('MERGE_SETTINGS_SCRIPT=','MERGE_CLAUDE_MCP_SCRIPT=')))+'\n'
    command+='DRY_RUN=false\nTIER=tier1\nplan() { :; }\nwarn() { printf "%s\\n" "$*" >&2; }\n'
    command+='cp() { "$PYTHON_BIN" -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' "$@" >> "$fixture_copy_log"; /bin/cp "$@"; }\n'
    command+=declarations
    for name in ('payload_command','copy_tree','is_optional_provider_payload','copy_core_tree','install_child_mcp_proxy','install_payload'):
        command+=shell_function(text,name)
    command+='install_payload\n'
    result=subprocess.run(['/bin/bash','-c',command],text=True,capture_output=True,timeout=60,
                          env={**os.environ,'HOME':str(install.parent/'home')})
    assert result.returncode==0,result.stderr
    sources=_copied_sources(setup['fixture_copy_log'],install)
    assert sources and all(path.is_relative_to(ROOT) for path in sources.values())
    for relative,source in sources.items():
        target=install/relative
        assert not source.is_symlink() and not target.is_symlink(), 'fixture payload must be regular files'
        assert source.read_bytes()==target.read_bytes(), 'installer copy differs from checkout source'
    (install/'env.sh').write_text('# Fixture-only; explicit client selection, no live defaults.\n')
    (install/'env.sh').chmod(0o600)
    owned_files,owned_dirs=installer_manifest_inventory(install)
    manifest={'schema_version':1,'install_dir':str(install),'owned_files':owned_files,
              'owned_dirs':owned_dirs,'skill_links':[]}
    # The initial manifest is fixture data obtained from the actual inventory;
    # subsequent configure/apply uses the real publisher/mutex/CAS unchanged.
    path=install/'install-state.json'
    path.write_text(json.dumps(manifest,sort_keys=True));path.chmod(0o600)
    return {'root':install,'sources':sources,'manifest':manifest,
            'source_digests':{rel:hashlib.sha256(source.read_bytes()).hexdigest() for rel,source in sources.items()}}
