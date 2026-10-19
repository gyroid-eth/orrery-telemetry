"""Read-only explicit-configure producers; no credential/CLI execution.

Outputs join install-state via runtime_configure_ownership. External resources
are evidence only, never installer-owned or candidates for automatic deletion.
"""
import hashlib
import os
from pathlib import Path
import stat

from local_model_auth import (CONTRACT_VERSION, LocalModelError, private_directory, secure_read,
                              executable_evidence as auth_executable_evidence,
                              source_evidence, skill_source_evidence)

INVALID = 'LOCAL_MODEL_CONFIGURE_RESOURCE_INVALID'


def canonical_path(value):
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or str(path) != str(value)
            or str(path.resolve()) != str(path)):
        raise LocalModelError(INVALID)
    return path


def executable_evidence(path, *, system_owner=False):
    """Pin a reviewed executable without running it, including version probes."""
    path = canonical_path(path)
    try:
        evidence = auth_executable_evidence(path)
        if not system_owner and evidence['uid'] != os.getuid():raise ValueError()
        return evidence
    except (OSError,ValueError,LocalModelError):
        raise LocalModelError(INVALID) from None


def runner_output(install_root, python):
    """One reviewed stdlib runner, absolute isolated interpreter, digest directory."""
    root = canonical_path(install_root)
    interpreter = executable_evidence(python,system_owner=True)
    # Kernel shebang parsing cannot represent spaces or arbitrary argv. Do not
    # silently fall back to env/PATH or a shell wrapper with different semantics.
    if any(character.isspace() for character in interpreter['path']):
        raise LocalModelError(INVALID)
    header = '#!'+interpreter['path']+' -I\n'
    if len(header.encode()) > 240:raise LocalModelError(INVALID)
    source = Path(__file__).with_name('local_model_auth.py')
    data = secure_read(source,os.getuid(),limit=1048576)
    content = header + data.decode('utf-8')
    digest = hashlib.sha256(content.encode()).hexdigest()
    path = root/'libexec/local-model-auth'/digest/'runner'
    return {'output':{'path':str(path),'content':content,'mode':0o500,'kind':'auth-runner'},
            'runner':{'path':str(path),'sha256':digest,'contract_version':CONTRACT_VERSION,
                      'dependency_digests':{},'python':interpreter},
            'source_sha256':hashlib.sha256(data).hexdigest(),'python':interpreter}


def resource_digests(paths):
    """Operator-declared nonsecret dependencies; never glob or execute auth."""
    if (not isinstance(paths,list) or len(paths)>256
            or any(not isinstance(path,str) for path in paths)
            or len(set(paths)) != len(paths)):
        raise LocalModelError(INVALID)
    try:
        return {str(canonical_path(path)):hashlib.sha256(
            secure_read(canonical_path(path),os.getuid())).hexdigest() for path in paths}
    except (OSError,ValueError,TypeError):
        raise LocalModelError(INVALID) from None


def settings_sources(paths):
    """Pin explicit present AND absent sources without loading their settings.

    Source enumeration/precedence/native interpretation belongs to the existing
    provider configure plan. This is not a settings merger or proof of native
    permissions; an incomplete/unknown source set must be refused by that plan.
    Returned records contain metadata/digests only, never settings or tokens.
    """
    if (not isinstance(paths,list) or len(paths)>256 or
            any(not isinstance(path,str) for path in paths) or len(set(paths)) != len(paths)):
        raise LocalModelError(INVALID)
    try:return source_evidence(paths)
    except LocalModelError:raise LocalModelError(INVALID) from None


def auth_source(profile, provider, dependencies):
    """Pin the declared external helper, never own/copy/execute it or its inputs."""
    try:
        adapter = profile[provider]
        command = adapter.get('auth_command')
        if command is None:
            if dependencies:raise ValueError()
            return None
        return {'executable':executable_evidence(command[0],system_owner=True),
                'dependency_digests':resource_digests(dependencies)}
    except (KeyError,IndexError,TypeError,ValueError):raise LocalModelError(INVALID) from None


def skill_outputs(sources, destination):
    """Link only explicitly selected canonical source directories, not HOME copies.

    Normal/local source equality still needs native acceptance; these hashes
    only detect source/link drift, never claim effective CLI skill visibility.
    """
    destination = canonical_path(destination)
    if not isinstance(sources,list) or len(sources)>256:
        raise LocalModelError(INVALID)
    outputs, evidence, names = [], {}, set()
    for value in sources:
        source = canonical_path(value)
        if source.name in names:raise LocalModelError(INVALID)
        names.add(source.name)
        try:
            fd = private_directory(source,os.getuid())
            os.close(fd)
        except (OSError,ValueError):raise LocalModelError(INVALID) from None
        outputs.append({'path':str(destination/source.name),'target':str(source),'kind':'skill-link'})
        try:evidence.update(skill_source_evidence([str(source)]))
        except LocalModelError:raise LocalModelError(INVALID) from None
    return {'outputs':outputs,'source_evidence':evidence}
