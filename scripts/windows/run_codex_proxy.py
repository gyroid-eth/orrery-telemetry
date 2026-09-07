"""Windows equivalent of run-mcp.sh for an explicitly bound CLI child."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from private_state import require_private


def proxy_environment(environment: dict[str, str]) -> dict[str, str]:
    env = environment.copy()
    mode = env.get('AGENTSTACK_MAIL_HTTP_BEARER_MODE', 'enabled')
    if mode not in ('enabled', 'disabled'):
        raise ValueError('Mail bearer mode must be enabled or disabled')
    if mode == 'disabled':
        env.pop('MCP_AGENT_MAIL_TOKEN', None)
    elif not env.get('MCP_AGENT_MAIL_TOKEN'):
        path = Path(env.get('AGENTSTACK_MAIL_ENV', ''))
        if not path.is_file():
            raise ValueError('Authenticated Mail requires an explicit private AGENTSTACK_MAIL_ENV')
        require_private(path)
        for raw in path.read_text(encoding='utf-8').splitlines():
            key, separator, value = raw.strip().removeprefix('export ').partition('=')
            if separator and key.strip() == 'HTTP_BEARER_TOKEN':
                env['MCP_AGENT_MAIL_TOKEN'] = value.strip().strip('\"\'')
        if not env.get('MCP_AGENT_MAIL_TOKEN'):
            raise ValueError('Mail env does not contain HTTP_BEARER_TOKEN')
    return env


def verify_direct_token(environment: dict[str, str]) -> None:
    """Refuse a direct binding whose owner-token file lost its private ACL."""

    if not environment.get('AGENTSTACK_PROXY_AGENT_NAME'):
        return
    value = environment.get('AGENTSTACK_PROXY_TOKEN_FILE', '').strip()
    path = Path(value)
    if not value or not path.is_absolute() or not path.is_file():
        raise ValueError('Direct Mail binding requires an existing absolute owner token file')
    require_private(path)


def main() -> None:
    if sys.platform != 'win32':
        raise RuntimeError('This proxy entry requires native Windows')
    env = proxy_environment(dict(os.environ))
    verify_direct_token(env)
    os.environ.clear()
    os.environ.update(env)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'integrations/codex_app/src'))
    from agentstack_codex_app.mcp_server import serve
    serve()


if __name__ == '__main__':
    main()
