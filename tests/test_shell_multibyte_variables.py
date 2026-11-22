"""日本語localeのbash 3.2でも変数名の終端を明示する。"""
from pathlib import Path
import os
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNBRACED_MULTIBYTE = re.compile(r'\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]')
SHELL_SHEBANG = re.compile(r'^#!.*(?:/|\s)(?:bash|sh|dash|zsh|ksh)(?:\s|$)')


def shell_files():
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    for name in tracked:
        path = ROOT / name
        if not name or not path.is_file():
            continue
        if path.suffix == '.sh':
            yield path
        elif Path(name).parts[0] in {'bin', 'hooks'}:
            first = path.read_text(encoding='utf-8').splitlines()[:1]
            if first and SHELL_SHEBANG.match(first[0]):
                yield path


def test_shell_variables_are_braced_before_non_ascii():
    violations = []
    for path in shell_files():
        for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if UNBRACED_MULTIBYTE.search(line):
                violations.append(f'{path.relative_to(ROOT)}:{line_number}: {line.strip()}')
    assert not violations, '非ASCIIの直前では${NAME}を使ってください:\n' + '\n'.join(violations)


@pytest.mark.parametrize('text,unsafe', [
    ('echo "$NAME（説明）"', True), ('echo "$NAME。"', True),
    ('echo "$NAME_2日本語"', True), ('echo "${NAME}（説明）"', False),
    ('echo "$NAME/path"', False),
])
def test_multibyte_variable_detector(text, unsafe):
    assert bool(UNBRACED_MULTIBYTE.search(text)) == unsafe


@pytest.mark.parametrize('source,marker,variable,value,expected', [
    ('scripts/install.sh', 'plan "DeepSeek opt-in:', 'DSH_OPT_IN', 'enable',
     'DeepSeek opt-in: enable（runtime・Node・鍵の取得なし）'),
    ('hooks/session-start-reminder.sh', 'echo "ERROR: ', 'SHELL_REGISTRATION_ERROR', 'fixture',
     'ERROR: fixture。identity split'),
])
def test_message_expansion_in_japanese_locale_on_bash32(source, marker, variable, value, expected):
    """実際の表示行だけを実行し、installerや登録hook本体は起動しない。"""
    bash = Path('/bin/bash')
    if not bash.exists() or 'version 3.2' not in subprocess.check_output([str(bash), '--version'], text=True):
        pytest.skip('/bin/bash 3.2 is unavailable')
    if 'ja_JP.UTF-8' not in subprocess.check_output(['locale', '-a'], text=True).splitlines():
        pytest.skip('ja_JP.UTF-8 is unavailable')
    lines = [line.strip() for line in (ROOT / source).read_text().splitlines()
             if marker in line and variable in line]
    assert len(lines) == 1
    script = 'set -u\nplan(){ printf "%s\\n" "$*"; }\n' + variable + '=' + value + '\n' + lines[0]
    result = subprocess.run([str(bash), '-c', script], capture_output=True, text=True, errors='replace',
                            env={**os.environ, 'LC_ALL': 'ja_JP.UTF-8', 'LC_CTYPE': 'ja_JP.UTF-8'},
                            timeout=10)
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
