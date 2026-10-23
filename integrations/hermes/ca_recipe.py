"""Owned CA input recipe and static evidence checks; no native imports or processes."""
import email.parser
import hashlib
import json
from pathlib import Path
import re
import tomllib

FILES = ('pyproject.toml', 'uv.lock', 'pm/lock.json')
BASE = 'd923a5eb487a17c0a68801bba9ef0f56c81040a8'
DONOR = '75a8d6fcf474d2e87229d22a9c114cda97d8bb71'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Hermes CA evidence is not a regular file')
    return digest(path.read_bytes())


def load(repo):
    directory = Path(repo) / 'integrations/hermes'
    raw = (directory / 'ca-inputs.json').read_bytes()
    policy = json.loads(raw)
    patch = (directory / 'ca-inputs.patch').read_bytes()
    if (policy['base_commit'] != BASE or policy['donor_commit'] != DONOR
            or set(policy['inputs']) != set(FILES) or digest(patch) != policy['patch_sha256']
            or policy['certifi']['version'] != '2026.10.19' or policy['uv_version'] != '0.13.5'):
        raise ValueError('Hermes CA recipe identity mismatch')
    return policy, patch, digest(raw + patch)


def verify_inputs(source, policy, phase):
    return {name: _checked_input(contained_file(Path(source) / name, source), policy['inputs'][name][phase])
            for name in FILES}


def _checked_input(path, expected):
    actual = file_digest(path)
    if actual != expected:
        raise ValueError('Hermes CA input hash mismatch: ' + path.name)
    return actual


def apply(repo, source):
    policy, patch, recipe = load(repo)
    verify_inputs(source, policy, 'before')
    chunks = re.split(r'(?=^--- a/)', patch.decode('utf-8'), flags=re.M)
    outputs = {}
    for chunk in filter(None, chunks):
        lines = chunk.splitlines(keepends=True)
        name = lines[0][6:].rstrip('\n')
        if name not in FILES or name in outputs or lines[1] != '+++ b/' + name + '\n':
            raise ValueError('Hermes CA patch path mismatch')
        before = (Path(source) / name).read_text(encoding='utf-8').splitlines(keepends=True)
        after, cursor, index = [], 0, 2
        while index < len(lines):
            match = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n', lines[index])
            if not match:
                raise ValueError('Hermes CA patch hunk mismatch')
            offset = max(0, int(match[1]) - 1)
            if offset < cursor or offset > len(before):
                raise ValueError('Hermes CA patch offset mismatch')
            after.extend(before[cursor:offset])
            cursor, index, old_count, new_count = offset, index + 1, 0, 0
            while index < len(lines) and not lines[index].startswith('@@ '):
                line = lines[index]
                if line.startswith((' ', '-')):
                    if cursor >= len(before) or before[cursor] != line[1:]:
                        raise ValueError('Hermes CA patch context mismatch')
                    cursor += 1
                    old_count += 1
                if line.startswith((' ', '+')):
                    after.append(line[1:])
                    new_count += 1
                if not line.startswith((' ', '-', '+')):
                    raise ValueError('Hermes CA patch line mismatch')
                index += 1
            if (old_count, new_count) != (int(match[2] or 1), int(match[4] or 1)):
                raise ValueError('Hermes CA patch length mismatch')
        after.extend(before[cursor:])
        data = ''.join(after).encode('utf-8')
        if digest(data) != policy['inputs'][name]['after']:
            raise ValueError('Hermes CA patch postimage mismatch')
        outputs[name] = data
    if set(outputs) != set(FILES):
        raise ValueError('Hermes CA patch input set mismatch')
    # Validate every output before writing anything in the unpublished generation.
    for name, data in outputs.items():
        (Path(source) / name).write_bytes(data)
    verify_inputs(source, policy, 'after')
    return recipe


def target_artifact(policy, target, source):
    if target not in policy['uv_artifacts']:
        raise ValueError('Hermes CA recipe does not support this target: ' + target)
    expected = policy['uv_artifacts'][target]
    tool = json.loads((Path(source) / 'pm/lock.json').read_text())['packages']['uv']
    if (tool['version'] != policy['uv_version']
            or tool['artifacts'].get(target) != {key: expected[key] for key in ('url', 'sha256')}):
        raise ValueError('Hermes CA uv artifact mismatch')
    return expected


def contained_file(path, root):
    path, root = Path(path), Path(root).resolve()
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError('Hermes CA evidence path escapes its generation')
    return path.resolve()


def runtime_evidence(repo, root, source, generation, target, paths):
    """Read files only. Used after acquisition and during catalog/reuse checks."""
    policy, _, recipe = load(repo)
    root, source, generation = Path(root).resolve(), Path(source).resolve(), Path(generation).resolve()
    if source != root / 'source':
        raise ValueError('Hermes CA source layout mismatch')
    key = hashlib.sha256(str(source).encode()).hexdigest()[:16]
    install = root / 'installs' / key
    if not generation.is_relative_to((install / 'environments').resolve()):
        raise ValueError('Hermes CA runtime layout mismatch')
    inputs = verify_inputs(source, policy, 'after')
    artifact = target_artifact(policy, target, source)
    facts_path = contained_file(install / 'facts.json', root)
    facts = json.loads(facts_path.read_text())['packages']['venv']
    if Path(facts['environment']).resolve() != generation:
        raise ValueError('Hermes CA runtime selection changed')
    resolved = contained_file(facts['resolved_lock'], install / 'environments')
    lock = tomllib.loads(resolved.read_text())
    certifi = [row for row in lock['package'] if row['name'] == 'certifi']
    if len(certifi) != 1 or certifi[0] != policy['certifi']['lock_record']:
        raise ValueError('Hermes CA resolved lock changed')
    bundle = contained_file(paths['bundle'], generation)
    metadata = contained_file(paths['metadata'], generation)
    info = email.parser.BytesParser().parsebytes(metadata.read_bytes())
    if (str(info['Name'] or '').lower() != 'certifi' or info['Version'] != policy['certifi']['version']
            or file_digest(bundle) != policy['certifi']['cacert_sha256']):
        raise ValueError('Hermes CA committed bundle/version mismatch')
    tool_facts_path = contained_file(root / 'tools/facts.json', root)
    uv = json.loads(tool_facts_path.read_text())['packages']['uv']
    entry = Path(uv['entry'])
    if (entry.is_absolute() or '..' in entry.parts or uv.get('target') != target
            or uv.get('version') != policy['uv_version'] or uv.get('artifacts') != [artifact['sha256']]):
        raise ValueError('Hermes CA realized uv identity mismatch')
    executable = contained_file(root / 'tools' / entry / 'uv', root / 'tools')
    if file_digest(executable) != artifact['executable_sha256']:
        raise ValueError('Hermes CA realized uv binary mismatch')
    stamp = contained_file(source / 'install-stamp.json', source)
    provenance = json.loads(stamp.read_text())
    if (provenance.get('commit') != BASE or provenance.get('dirty') is not True
            or provenance.get('source') != 'local' or provenance.get('updateMechanism') != 'external'
            or provenance.get('payload') != 'bootstrap' or provenance.get('runtimeDir') is not None):
        raise ValueError('Hermes CA source provenance mismatch')
    return {'recipe': recipe, 'base_commit': BASE, 'donor_commit': DONOR, 'target': target,
            'inputs': inputs, 'runtime': str(generation), 'resolved_lock': str(resolved),
            'resolved_lock_sha256': file_digest(resolved),
            'bundle': str(bundle), 'bundle_sha256': file_digest(bundle),
            'metadata': str(metadata), 'metadata_sha256': file_digest(metadata),
            'uv_binary': str(executable), 'uv_binary_sha256': file_digest(executable),
            'uv_artifact_sha256': artifact['sha256'], 'stamp_sha256': file_digest(stamp)}


def validate_static(repo, details):
    proof = details['ca_evidence']
    root, source = Path(details['managed_root']), Path(details['hermes_source'])
    generation = Path(details['hermes_executable']).parent.parent
    actual = runtime_evidence(repo, root, source, generation, proof['target'], proof)
    if proof != actual or details['ca_recipe'] != actual['recipe']:
        raise ValueError('Hermes CA evidence changed')
    return actual

