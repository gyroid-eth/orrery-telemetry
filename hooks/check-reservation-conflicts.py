#!/usr/bin/env python3
"""Validate the server's scoped conflict certificate from the renew response.

Pattern overlap and ACTIVE/exclusive selection belong to Mail, not this hook.
No resource read or second network/snapshot is involved.
"""
from __future__ import annotations


def validate_conflict_check(value, project, paths):
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1 or value.get('project_key') != project
            or value.get('paths') != paths or not isinstance(value.get('conflicts'), list)):
        raise ValueError('missing or invalid scoped reservation conflict check; update Mail before these hooks')
    owners = set()
    for row in value['conflicts']:
        if (not isinstance(row, dict) or set(row) != {'agent', 'path_pattern'}
                or any(not isinstance(row[key], str) or not row[key].strip() for key in row)):
            raise ValueError('invalid reservation conflict entry')
        owners.add(row['agent'])
    return sorted(owners)


if __name__ == '__main__':
    # A stale #269 caller must not treat this import-only validator as a
    # successful standalone resource check during a partial deployment.
    import sys
    print('Reservation conflict check rejected: update the guard and Mail together; validate the renew response', file=sys.stderr)
    raise SystemExit(2)
