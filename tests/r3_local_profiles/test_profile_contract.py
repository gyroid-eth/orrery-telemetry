"""Design v3 §§1–2/6–7: fail closed before registration, network or tmux."""
from copy import deepcopy
import os
import tomllib

import pytest

from .support import MODEL, canonical_digest, profile_text


def load(profiles, workspace, ref="local:fixture", **kwargs):
    return profiles.load_profile(workspace.root, ref, expected_uid=os.getuid(), **kwargs)


def test_literal_profile_and_independent_digest(profiles, workspace):
    value = load(profiles, workspace)
    expected = tomllib.loads(profile_text())
    assert value == expected
    assert value["model"] == MODEL
    assert profiles.profile_digest(value) == canonical_digest(expected)


@pytest.mark.parametrize("ref", ["local:", "local:../fixture", "local:/fixture", "local:Fixture",
                                     "local:a.b", "local:https://fixture.invalid", "local:a/b",
                                     "local:" + "a" * 49, "local:fixture\x00"])
def test_selector_is_not_a_path_or_url(profiles, workspace, ref):
    with pytest.raises(profiles.LocalModelError, match="LOCAL_MODEL_PROFILE_INVALID"):
        load(profiles, workspace, ref)


@pytest.mark.parametrize("case", ["missing", "root-mode", "file-mode", "symlink", "hardlink",
                                     "symlink-root", "unsafe-parent", "non-regular", "wrong-uid"])
def test_filesystem_ownership_and_placement(profiles, workspace, case):
    root, file = workspace.root, workspace.profile
    expected_uid = os.getuid()
    if case == "missing":
        file.unlink()
    elif case == "root-mode":
        root.chmod(0o755)
    elif case == "file-mode":
        file.chmod(0o644)
    elif case == "symlink":
        real = root / "actual.toml"
        file.rename(real)
        file.symlink_to(real)
    elif case == "hardlink":
        os.link(file, root / "copy.toml")
    elif case == "symlink-root":
        real = root.with_name("actual-models")
        root.rename(real)
        root.symlink_to(real, target_is_directory=True)
    elif case == "unsafe-parent":
        root.parent.chmod(0o777)
    elif case == "non-regular":
        file.unlink()
        file.mkdir()
    else:
        # Check UID validation without chown/root privileges.
        expected_uid += 1
    reason = "LOCAL_MODEL_PROFILE_UNAVAILABLE" if case == "missing" else "LOCAL_MODEL_PROFILE_INVALID"
    with pytest.raises(profiles.LocalModelError, match=reason):
        profiles.load_profile(root, "local:fixture", expected_uid=expected_uid)


@pytest.mark.parametrize("old,new", [
    ('name = "fixture"', 'name = "other"'),
    ('model = "fixture-glm-model"', 'model = ""'),
    ('context_window = 131072', 'context_window = true'),
    ('context_window = 131072', 'context_window = -1'),
    ('max_output_tokens = 16384', 'max_output_tokens = 131072'),
    ('max_output_tokens = 16384', 'max_output_tokens = 0'),
    ('max_output_tokens = 16384', 'max_output_tokens = false'),
    ('schema_version = 1', 'schema_version = 999'),
    ('schema_version = 1', 'schema_version = 1\nunknown_field = "not-ignored"'),
    ('wire_api = "responses"', 'wire_api = "responses"\nunknown_adapter_field = true'),
    ('wire_api = "responses"', 'wire_api = "chat"'),
    ('https://inference.example.invalid/v1', 'http://remote.example.invalid/v1'),
    ('https://inference.example.invalid/v1', 'https://user:password@fixture.invalid/v1'),
    ('https://inference.example.invalid/v1', 'https://fixture.invalid/v1?api_key=forbidden'),
    ('https://inference.example.invalid/v1', 'https://fixture.invalid/v1#forbidden'),
    ('env = {}', 'env = { OPENAI_API_KEY = "forbidden" }'),
    ('env = {}', 'env = { PATH = "/caller/bin" }'),
    ('env = {}', 'env = { HOME = "/caller/home" }'),
    ('env = {}', 'env = { TMUX = "forbidden" }'),
    ('env = {}', 'env = { AGENTSTACK_MODE = "legacy" }'),
    ('env = {}', 'env = { LD_PRELOAD = "forbidden" }'),
    ('env = {}', 'env = { ANTHROPIC_MODEL = "other" }'),
    ('env = {}', 'env = { NOT_AN_ALLOWLISTED_OPTION = "not-ignored" }'),
    ('env = {}', 'auth_command = "echo forbidden"\nenv = {}'),
    ('env = {}', 'auth_command = ["relative-helper"]\nenv = {}'),
])
def test_schema_rejects_instead_of_silently_dropping(profiles, workspace, old, new):
    workspace.profile.write_text(profile_text().replace(old, new))
    with pytest.raises(profiles.LocalModelError, match="LOCAL_MODEL_PROFILE_INVALID"):
        load(profiles, workspace)


def test_adapter_required_and_model_id_not_gpt_filtered(profiles, workspace):
    workspace.profile.write_text(profile_text().split("[codex]")[0])
    with pytest.raises(profiles.LocalModelError, match="LOCAL_MODEL_PROFILE_INVALID"):
        load(profiles, workspace)
    workspace.profile.write_text(profile_text().replace(MODEL, "vendor/glm:revision-1"))
    assert load(profiles, workspace)["model"] == "vendor/glm:revision-1"


@pytest.mark.parametrize("adapter,key,value", [
    ("codex", "base_url", "https://other.invalid/v1"),
    ("claude", "base_url", "https://other.invalid"),
    ("codex", "auth_command", ["/fixture/helper", "service-a"]),
    ("claude", "auth_command", ["/fixture/helper", "service-b"]),
    ("claude", "env", {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}),
])
def test_digest_covers_whole_profile_not_only_selected_adapter(profiles, workspace, adapter, key, value):
    original = load(profiles, workspace)
    changed = deepcopy(original)
    changed[adapter][key] = value
    assert profiles.profile_digest(changed) == canonical_digest(changed)
    assert profiles.profile_digest(changed) != profiles.profile_digest(original)


def test_digest_ignores_toml_presentation_but_keeps_argv_order(profiles, workspace):
    first = load(profiles, workspace)
    workspace.profile.write_text("# harmless comment\n\n" + profile_text().replace(
        'schema_version = 1\nname = "fixture"', 'name = "fixture"\nschema_version = 1'))
    assert profiles.profile_digest(load(profiles, workspace)) == profiles.profile_digest(first)
    a = {**first, "codex": {**first["codex"], "auth_command": ["/fixture/helper", "a", "b"]}}
    b = {**first, "codex": {**first["codex"], "auth_command": ["/fixture/helper", "b", "a"]}}
    assert profiles.profile_digest(a) != profiles.profile_digest(b)


@pytest.mark.parametrize("value", ['"bad\\u0000model"', '"bad\\nmodel"', '"bad\\u001fmodel"'])
def test_model_control_characters_are_invalid(profiles, workspace, value):
    workspace.profile.write_text(profile_text().replace('"' + MODEL + '"', value))
    with pytest.raises(profiles.LocalModelError, match="LOCAL_MODEL_PROFILE_INVALID"):
        load(profiles, workspace)


@pytest.mark.parametrize("field", ["schema_version", "name", "model", "context_window", "max_output_tokens"])
def test_required_common_fields_cannot_be_missing(profiles, workspace, field):
    workspace.profile.write_text("\n".join(line for line in profile_text().splitlines() if not line.startswith(field + " =")))
    with pytest.raises(profiles.LocalModelError, match="LOCAL_MODEL_PROFILE_INVALID"):
        load(profiles, workspace)


@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_one_adapter_profile_is_valid(profiles, workspace, adapter):
    common, rest = profile_text().split("[codex]", 1)
    codex, claude = rest.split("[claude]", 1)
    selected = common + ("[codex]" + codex if adapter == "codex" else "[claude]" + claude)
    workspace.profile.write_text(selected)
    value = load(profiles, workspace)
    assert adapter in value and ("claude" if adapter == "codex" else "codex") not in value


@pytest.mark.parametrize("old,new", [
    ('wire_api = "anthropic-messages"', 'wire_api = "chat"'),
    ('base_url = "https://inference.example.invalid"', 'base_url = "http://remote.invalid"'),
    ('base_url = "https://inference.example.invalid"', 'base_url = "https://user:secret@fixture.invalid"'),
    ('base_url = "https://inference.example.invalid"', 'base_url = "https://fixture.invalid?key=forbidden"'),
    ('env = { CLAUDE_AUTOCOMPACT_PCT_OVERRIDE = "85" }', 'env = { ANTHROPIC_AUTH_TOKEN = "forbidden" }'),
    ('env = { CLAUDE_AUTOCOMPACT_PCT_OVERRIDE = "85" }', 'env = { CLAUDE_CONFIG_DIR = "/caller" }'),
])
def test_claude_adapter_negatives_are_not_inferred_from_codex(profiles, workspace, old, new):
    workspace.profile.write_text(profile_text().replace(old, new))
    with pytest.raises(profiles.LocalModelError, match="LOCAL_MODEL_PROFILE_INVALID"):
        load(profiles, workspace)
