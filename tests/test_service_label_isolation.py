"""A test run must not be able to uninstall the product from this machine.

launchd labels are not namespaced by ``HOME``. A test can point ``HOME`` at a
temporary directory and still reach ``gui/<uid>/org.agentstack.agentdashboard``
— the label a real install of this product owns. ``agentctl.sh stop`` in a test
teardown then boots out the developer's own dashboard, and the suite reports
green while the machine loses its service.

That happened: ``test_installer_reuses_existing_agent_mail_listener_database``
removed a working install, and nothing noticed because until the product was
installed on a development machine there was nothing there to remove.

``service_teardown`` already documents the rule — pass a test-owned label. This
makes the rule enforceable instead of remembered, because the failure it
prevents is silent, is invisible on CI (where nothing is installed), and only
appears on the one machine where someone is dogfooding.
"""

from __future__ import annotations

import ast
import pathlib


TESTS_DIR = pathlib.Path(__file__).resolve().parent
PRODUCTION_PREFIX = "org.agentstack"
HOME_KEY = "AGENTSTACK_HOME"
LABEL_KEY = "AGENTSTACK_LABEL_PREFIX"


def _static_prefix(node: ast.expr) -> str | None:
    """The part of the value known without running it, or None if opaque."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        # e.g. TEST_LABEL_PREFIX imported from service_teardown.
        return None if node.id != "PRODUCTION_PREFIX" else PRODUCTION_PREFIX
    if isinstance(node, ast.JoinedStr):
        head = node.values[0] if node.values else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return None


def _env_dicts_with_home(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if HOME_KEY in keys:
            yield node


def _label_value(node: ast.Dict) -> ast.expr | None:
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == LABEL_KEY:
            return value
    return None


def test_no_test_env_can_boot_out_the_installed_service():
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for env in _env_dicts_with_home(tree):
            where = f"{path.name}:{env.lineno}"
            label = _label_value(env)
            if label is None:
                offenders.append(
                    f"{where}: sets {HOME_KEY} without {LABEL_KEY}; a teardown "
                    f"here boots out {PRODUCTION_PREFIX}.agentdashboard"
                )
                continue
            prefix = _static_prefix(label)
            if prefix is None:
                continue  # a name such as TEST_LABEL_PREFIX; checked below.
            if prefix == PRODUCTION_PREFIX or not prefix.startswith(
                f"{PRODUCTION_PREFIX}."
            ):
                offenders.append(
                    f"{where}: {LABEL_KEY}={prefix!r} is the label a real "
                    "install owns; use a test-specific suffix"
                )
    assert not offenders, "tests that can uninstall this machine's dashboard:\n" + (
        "\n".join(offenders)
    )


def test_the_shared_test_prefix_is_not_the_production_label():
    """The positive control: the rule above is only worth having if the name
    the tests share is itself distinct from what a real install registers."""
    from service_teardown import TEST_LABEL_PREFIX

    assert TEST_LABEL_PREFIX != PRODUCTION_PREFIX
    assert TEST_LABEL_PREFIX.startswith(f"{PRODUCTION_PREFIX}.")
