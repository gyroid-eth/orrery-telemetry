"""The managed dsh launcher must start whatever the machine load is."""
from pathlib import Path

LAUNCH = Path(__file__).resolve().parents[1] / 'integrations' / 'dsh' / 'launch.py'


def test_launcher_has_no_load_gate():
    source = LAUNCH.read_text(encoding='utf-8')
    assert 'getloadavg' not in source
    assert 'Load is at least' not in source
