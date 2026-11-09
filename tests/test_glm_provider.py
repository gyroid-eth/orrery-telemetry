"""Shared numeric model spelling and Local GLM maker presentation."""
import pytest

from agentstack_mail.model_normalize import display_model, normalize_model
import dashboard.server as server


@pytest.mark.parametrize('raw,canonical,display,provider', [
    ('glm-5.3-flash', 'glm-5.3-flash', 'GLM 5.3 flash', 'zai'),
    ('zai-org/GLM-4.6', 'glm-4.6', 'GLM 4.6', 'zai'),
    ('facebook/xglm-7.5B', 'facebook/xglm-7.5B', 'facebook/xglm-7.5B', ''),
    ('chatglm3-6b', 'chatglm3-6b', 'chatglm3-6b', ''),
    ('zai-org/GLM-5.3-Flash', 'glm-5.3-flash', 'GLM 5.3 flash', 'zai'),
    ('deepseek-v4.1-flash', 'deepseek-4.1-flash', 'DeepSeek 4.1 flash', 'deepseek'),
    ('deepseek-ai/DeepSeek-V3', 'deepseek-3', 'DeepSeek 3', 'deepseek'),
    ('claude-opus-4-7[1m]', 'opus-4.7', 'Opus 4.7', 'anthropic'),
    ('gemini-2.5-pro', 'gemini-2.5-pro', 'Gemini 2.5 pro', 'google'),
    ('qwen3.8-flash-next-iq3_xxs', 'qwen-3.8-flash', 'Qwen 3.8 flash', 'alibaba'),
    ('deepseek-3.2', 'deepseek-3.2', 'DeepSeek 3.2', 'deepseek'),
])
def test_mail_and_dashboard_numeric_model_spelling(raw, canonical, display, provider):
    assert normalize_model(raw) == server._normalize_model(raw) == canonical
    assert display_model(raw) == server._display_model(raw) == display
    assert server._provider_of(raw) == provider


def test_glm_asset_is_public_independently_of_deepseek_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'HERE', str(tmp_path))
    for enabled in (False, True):
        (tmp_path / 'dsh-provider.json').write_text('{"enabled":' + str(enabled).lower() + '}')
        assert server.is_public_static('/assets/glm.svg')
        assert server.is_public_static('/assets/deepseek.svg') is enabled


def test_dashboard_full_gpt_label_does_not_change_mail_serialization():
    assert normalize_model('gpt-5-codex') == server._normalize_model('gpt-5-codex') == 'gpt-5-codex'
    assert display_model('gpt-5-codex') == 'GPT 5 codex'
    assert server._display_model('gpt-5-codex') == 'GPT-5-Codex'
    assert server._provider_of('gpt-5-codex') == 'openai'
