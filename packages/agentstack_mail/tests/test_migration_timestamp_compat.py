"""Cross-platform Git ISO-8601 timestamp compatibility."""

from agentstack_mail.migration import _normalize_git_iso8601


def test_git_utc_timestamp_accepts_z_and_explicit_offset() -> None:
    expected = "2026-09-04T00:57:54+00:00"

    assert _normalize_git_iso8601("2026-09-04T00:57:54Z") == expected
    assert _normalize_git_iso8601(expected) == expected
