"""Project native telemetry answers and MCP selection without rewriting other YAML bytes.

Called only for the selected product profile while thin_config holds its lock.
This uses configuration data; it never imports Hermes or runs a helper.
"""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace


ANSWER = {'enabled': False, 'send': False, 'offer_version': 2}


def _mapping(node):
    if node is None or node.tag == 'tag:yaml.org,2002:null':
        return {}
    if node.tag != 'tag:yaml.org,2002:map':
        raise ValueError('invalid_telemetry_config')
    return {key.value: value for key, value in node.value}


def _edits(text, node, fields):
    if node is None:
        return [(len(text), len(text), ('\n' if text and not text.endswith('\n') else '') +
                 '\n'.join(key + ': ' + json.dumps(value) for key, value in fields.items()) + '\n')]
    if node.tag == 'tag:yaml.org,2002:null':
        return [(node.start_mark.index, node.end_mark.index, json.dumps(fields))]
    children = _mapping(node)
    edits, missing = [], {}
    for key, value in fields.items():
        if key not in children:
            missing[key] = value
        elif isinstance(value, dict):
            edits.extend(_edits(text, children[key], value))
        else:
            child = children[key]
            edits.append((child.start_mark.index, child.end_mark.index, json.dumps(value)))
    if missing:
        if node.flow_style:
            end = node.end_mark.index - 1
            separator = ' ' if text[:end].rstrip().endswith(('{', ',')) else ', '
            addition = separator + ', '.join(json.dumps(key) + ': ' + json.dumps(value)
                                             for key, value in missing.items())
        else:
            end = node.end_mark.index
            indent = node.value[0][0].start_mark.column
            addition = ('\n' if end and text[end - 1] != '\n' else '') + ''.join(
                ' ' * indent + (key if _yaml_parser().load(key) == key else json.dumps(key)) + ': ' + json.dumps(value) + '\n' for key, value in missing.items())
        edits.append((end, end, addition))
    return edits


def _yaml_parser():
    try:
        from ruamel.yaml import YAML
        parser = YAML(typ='safe')
        parser.version = (1, 1)
    except ModuleNotFoundError:
        # The repository development environment supplies PyYAML; Hermes's
        # committed runtime supplies ruamel. Both expose marked YAML nodes.
        import yaml
        parser = SimpleNamespace(load=yaml.safe_load, compose=yaml.compose)
    return parser


def settle_unanswered(profile, state):
    """Preserve explicit choices; add only the three native answer fields if needed."""
    parser = _yaml_parser()
    path = Path(profile) / 'config.yaml'
    if path.is_symlink():
        raise ValueError('owned_link_conflict')
    text = path.read_bytes().decode('utf-8') if path.exists() else ''
    config = parser.load(text) or {}
    if not isinstance(config, dict):
        raise ValueError('invalid_telemetry_config')
    telemetry = config.get('telemetry') or {}
    if not isinstance(telemetry, dict):
        raise ValueError('invalid_telemetry_config')
    section = telemetry.get('shared_metrics') or {}
    if not isinstance(section, dict):
        raise ValueError('invalid_telemetry_config')
    # An opt-in was always deliberate; a current-version opt-out is settled too.
    if section.get('enabled') is True or section.get('send') is True or (
            section.get('offer_version') == ANSWER['offer_version'] and
            ('enabled' in section or 'send' in section)):
        return False
    edits = _edits(text, parser.compose(text), {'telemetry': {'shared_metrics': ANSWER}})
    revised = text
    for start, end, value in sorted(edits, reverse=True):
        revised = revised[:start] + value + revised[end:]
    expected = copy.deepcopy(config)
    expected['telemetry'] = dict(expected.get('telemetry') or {})
    expected['telemetry']['shared_metrics'] = dict(expected['telemetry'].get('shared_metrics') or {})
    expected['telemetry']['shared_metrics'].update(ANSWER)
    if parser.load(revised) != expected:
        raise ValueError('invalid_telemetry_config')
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    try:
        with temporary.open('xb') as handle:
            os.chmod(temporary, 0o600)
            handle.write(revised.encode('utf-8'))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'telemetry-defaults.log').open('a', encoding='utf-8') as handle:
        handle.write(datetime.now(timezone.utc).isoformat() + ' defaulted unanswered shared metrics to no thanks\n')
    return True


def project_mcp(profile, servers):
    """Write only the selected private MCP section, preserving other YAML bytes.

    The caller has already validated selection/deep-merge conflicts under its
    profile lock. Native startup reads raw config before discovery; its actual
    loader also sees the same selection through the managed overlay.
    """
    parser = _yaml_parser()
    path = Path(profile) / 'config.yaml'
    if path.is_symlink():
        raise ValueError('owned_link_conflict')
    text = path.read_bytes().decode('utf-8') if path.exists() else ''
    config = parser.load(text) or {}
    if (not isinstance(config, dict) or not isinstance(servers, dict)
            or ('mcp_servers' in config and not isinstance(config['mcp_servers'], dict))):
        raise ValueError('invalid_mcp_config')
    expected = {**config, 'mcp_servers': servers}
    if config == expected:
        return False
    try:
        edits = _edits(text, parser.compose(text), {'mcp_servers': servers})
    except ValueError:
        raise ValueError('invalid_mcp_config') from None
    revised = text
    for start, end, value in sorted(edits, reverse=True):
        revised = revised[:start] + value + revised[end:]
    if parser.load(revised) != expected:
        raise ValueError('invalid_mcp_config')
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    try:
        with temporary.open('xb') as handle:
            os.chmod(temporary, 0o600)
            handle.write(revised.encode('utf-8'))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
