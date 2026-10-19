"""Fixture policy placement matches installation; source guards stay real."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from _installed_client_payload import ROOT, copy_client_library


@pytest.mark.parametrize('provider', ['codex','claude'])
def test_client_fixture_places_actual_policy_at_installed_relative_path(tmp_path,provider):
    root = tmp_path/'install'
    copy_client_library(root)
    target = root/'dashboard'/(provider+'_models.py')
    assert target.is_file() and not target.is_symlink()
    assert target.read_bytes() == (ROOT/'dashboard'/(provider+'_models.py')).read_bytes()


def test_client_fixture_preserves_explicit_policy_stub(tmp_path):
    root = tmp_path/'install'
    target = root/'dashboard/codex_models.py'
    target.parent.mkdir(parents=True)
    target.write_text('# explicit fixture input, never executed\n')
    before = target.read_bytes()
    copy_client_library(root)
    assert target.read_bytes() == before


@pytest.mark.parametrize('provider', ['codex','claude'])
def test_absent_installed_policy_cannot_fall_back_to_checkout_module(tmp_path,monkeypatch,provider):
    root = tmp_path/'install'
    copy_client_library(root)
    target = root/'dashboard'/(provider+'_models.py')
    if target.exists():target.unlink()
    script = root/'bin/lib/runtime_model_catalog.py'
    spec = importlib.util.spec_from_file_location('fixture_catalog_missing_'+provider,script)
    catalog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(catalog)
    monkeypatch.setitem(sys.modules,'dashboard.'+provider+'_models',
        SimpleNamespace(__file__=str(ROOT/'dashboard'/(provider+'_models.py'))))
    with pytest.raises(FileNotFoundError):catalog.provider_policy(provider)
