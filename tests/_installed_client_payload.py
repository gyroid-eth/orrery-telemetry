"""Build complete client libraries from the installer's own payload declarations.

Partial-install and missing-helper tests deliberately do not use this helper.
Fixture stubs already present in the destination remain under the test's control.
"""
from pathlib import Path
import re
import shutil


ROOT = Path(__file__).resolve().parents[1]


def runtime_contract_sources() -> tuple[str, ...]:
    text = (ROOT / "scripts/install.sh").read_text()
    declaration = re.search(
        r"^RUNTIME_SCHEMA_SOURCE=.*\nRUNTIME_FIXTURE_SOURCE=.*\n"
        r"RUNTIME_CLIENT_CONTRACT_SOURCES=\(\n.*?^\)\n",
        text, re.MULTILINE | re.DOTALL,
    )
    assert declaration, "installer client payload declaration missing"
    declared = re.findall(r'"\$REPO_ROOT/([^"\n]+)"', declaration.group())
    return tuple(declared)


def client_library_sources() -> tuple[str, ...]:
    text = (ROOT / "scripts/install.sh").read_text()
    direct = re.findall(
        r'cp "\$REPO_ROOT/(bin/lib/[^"\n]+)" "\$BIN_DIR/lib/[^"\n]+"', text
    )
    sources = tuple(dict.fromkeys(direct + list(runtime_contract_sources())))
    assert sources and len({Path(p).name for p in sources}) == len(sources)
    return sources


def copy_client_library(install_root: Path) -> None:
    library = install_root / "bin/lib"
    library.mkdir(parents=True, exist_ok=True)
    for relative in client_library_sources():
        target = library / Path(relative).name
        if not target.exists():
            shutil.copy2(ROOT / relative, target)
