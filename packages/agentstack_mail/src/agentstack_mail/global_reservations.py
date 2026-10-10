"""Schema5 reservations: one SQL authority, expiry is derived, never collected."""

from datetime import datetime, timedelta

from .global_server import GlobalError
from .global_s2a import utc
from .global_s2c_contract import FIXTURE, validate
from .reservation_paths import (
    normalize_paths,
    paths_overlap,
    ReservationError,
)

ACTIVE = "(released_ts IS NULL AND expires_ts > :now_utc)"
MAX_ID = 2**63 - 1


def lease_timestamp(value):
    try:
        return utc(
            value.isoformat() if isinstance(value, datetime) else value
        ).isoformat(timespec="microseconds")
    except (OverflowError, ValueError):
        raise GlobalError("TIMESTAMP_INVALID") from None


def expiry(value, seconds):
    try:
        return lease_timestamp(utc(lease_timestamp(value)) + timedelta(seconds=seconds))
    except (OverflowError, ValueError):
        raise GlobalError("TIMESTAMP_INVALID") from None


def projection(row):
    result = {
        k: row[k]
        for k in FIXTURE["lease_schema"]["properties"]
        if k != "owner_agent_id"
    }
    result.update(
        owner_agent_id=row["agent_id"],
        exclusive=bool(row["exclusive"]),
        path_unknown=bool(row["path_unknown"]),
    )
    validate(result, FIXTURE["lease_schema"], "RESPONSE_INVALID")
    return result


def normalize(values):
    try:
        return normalize_paths(values)
    except ReservationError as exc:
        reason = str(exc)
        if reason == "ABSOLUTE_PATH_REQUIRED":
            raise GlobalError(reason) from None
        raise GlobalError("ACTIVE_LEASE_RULES_UNKNOWN") from None


def scope(row):
    if row["path_unknown"]:
        raise GlobalError("ACTIVE_LEASE_RULES_UNKNOWN")
    return normalize([row["path_pattern"]])[0]


def overlap(left, right):
    try:
        return paths_overlap(left, right)
    except ReservationError:
        raise GlobalError("ACTIVE_LEASE_RULES_UNKNOWN") from None


def active_rows(db, stamp, owner=None):
    return db.execute(
        "SELECT * FROM file_reservations WHERE "
        + ACTIVE
        + (" AND agent_id=:owner" if owner is not None else "")
        + " ORDER BY id",
        {"now_utc": lease_timestamp(stamp), "owner": owner},
    ).fetchall()


def selected(db, owner, args, stamp, *, missing_paths_ok=False):
    ids, paths = args.get("file_reservation_ids"), args.get("paths")
    if ids is None and paths is None:
        rows = active_rows(db, stamp, owner)
    elif ids is not None:
        rows = []
        for key in dict.fromkeys(ids):
            row = db.execute(
                "SELECT * FROM file_reservations WHERE id=?", (key,)
            ).fetchone()
            if row is None:
                raise GlobalError("LEASE_NOT_FOUND")
            rows.append(row)
    else:
        patterns = [p.value for p in normalize(paths)]
        params = {f"pattern_{i}": value for i, value in enumerate(patterns)}
        marks = ",".join(":" + key for key in params)
        params.update(owner=owner, now_utc=lease_timestamp(stamp))
        rows = db.execute(
            "SELECT * FROM file_reservations WHERE agent_id=:owner AND "
            + ACTIVE
            + " AND path_pattern IN ("
            + marks
            + ") ORDER BY id",
            params,
        ).fetchall()
        if not missing_paths_ok and set(patterns) - {r["path_pattern"] for r in rows}:
            raise GlobalError("LEASE_NOT_FOUND")
    if any(r["agent_id"] != owner for r in rows):
        raise GlobalError("LEASE_OWNER_REQUIRED")
    if len(rows) > 100:
        raise GlobalError("LEASE_SELECTION_TOO_LARGE")
    return rows


def release(db, rows, stamp, cause="owner", note=""):
    for row in rows:
        if row["released_ts"] is None:
            db.execute(
                "UPDATE file_reservations SET released_ts=?,release_cause=?,release_note=?,revision=revision+1 WHERE id=?",
                (lease_timestamp(stamp), cause, note, row["id"]),
            )
    return [
        db.execute("SELECT * FROM file_reservations WHERE id=?", (r["id"],)).fetchone()
        for r in rows
    ]


def acquire(db, owner, values, stamp, *, ttl_seconds=3600, exclusive=True, reason=""):
    if not values:
        return [], []
    paths = list(dict.fromkeys(normalize(values)))
    until = expiry(stamp, ttl_seconds)
    rows = active_rows(db, stamp)
    # Validate every collision before the first state change. Expired history
    # is excluded by the indexed SQL predicate, irrespective of its size.
    for path in paths:
        for row in rows:
            if (
                row["agent_id"] != owner
                and (exclusive or row["exclusive"])
                and overlap(path, scope(row))
            ):
                raise GlobalError("RESERVATION_CONFLICT")
    maximum = db.execute(
        "SELECT COALESCE(MAX(id),0) FROM file_reservations"
    ).fetchone()[0]
    plans = []
    for path in paths:
        own = next(
            (
                r
                for r in rows
                if r["agent_id"] == owner
                and r["path_pattern"] == path.value
                and bool(r["exclusive"]) == exclusive
            ),
            None,
        )
        if own is not None and own["path_unknown"]:
            raise GlobalError("ACTIVE_LEASE_RULES_UNKNOWN")
        if own is None:
            maximum += 1
            if maximum > MAX_ID:
                raise GlobalError("LEASE_ID_CAPACITY_REACHED")
        plans.append((path, own, own["id"] if own else maximum))
    new_ids, granted = [], []
    for path, own, key in plans:
        if own:
            db.execute(
                "UPDATE file_reservations SET expires_ts=?,revision=revision+1 WHERE id=?",
                (max(own["expires_ts"], until), key),
            )
        else:
            db.execute(
                "INSERT INTO file_reservations(id,legacy_project_id,agent_id,path_pattern,exclusive,reason,created_ts,expires_ts,released_ts,legacy_path_pattern,path_unknown,revision,release_cause,release_note) VALUES (?,NULL,?,?,?,?,?,?,NULL,NULL,0,0,NULL,'')",
                (
                    key,
                    owner,
                    path.value,
                    exclusive,
                    reason,
                    lease_timestamp(stamp),
                    until,
                ),
            )
            new_ids.append(key)
        granted.append(
            db.execute("SELECT * FROM file_reservations WHERE id=?", (key,)).fetchone()
        )
    return granted, new_ids


def renew(db, rows, stamp, seconds):
    boundary = lease_timestamp(stamp)
    if any(r["released_ts"] is not None or r["expires_ts"] <= boundary for r in rows):
        raise GlobalError("LEASE_EXPIRED")
    plans = [(expiry(r["expires_ts"], seconds), r["id"]) for r in rows]
    db.executemany(
        "UPDATE file_reservations SET expires_ts=?,revision=revision+1 WHERE id=?",
        plans,
    )
    return [
        db.execute("SELECT * FROM file_reservations WHERE id=?", (r["id"],)).fetchone()
        for r in rows
    ]
