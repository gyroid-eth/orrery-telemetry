"""Regression coverage for the declared Python 3.10 support floor."""

from __future__ import annotations

import datetime

import agentstack_mail  # noqa: F401  # installs 3.10 stdlib compatibility aliases


def test_datetime_utc_alias_is_available() -> None:
    assert datetime.UTC is datetime.timezone.utc


def test_tomllib_api_is_available() -> None:
    import tomllib

    assert tomllib.loads("value = 1\n") == {"value": 1}
