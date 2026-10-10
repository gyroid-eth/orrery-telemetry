# Absolute reservation candidate (PR2)

[日本語](absolute-reservations.md)

## Scope

Shared normalization and explicit proxy/hook/CLI adapters remain. The only candidate reservation server is the [S2c SQLite runtime](global-reservations.en.md). PR2's in-memory ReservationServer, PersistentReservations copy/flush and reservation_activity probe/collector are removed. Default Mail/proxy/hooks remain legacy with activation=false. Candidate adapters require explicit fixture transport injection.

## Shared normalization

Server, client, CLI and hook use `reservation_paths.normalize_path()`. The server requires absolute paths. Clients explicitly provide their observed cwd for relative inputs. Legacy `project_key` is accepted and ignored; it never supplies an anchor. Unknown legacy anchors require reacquisition or migration evidence. This PR does not convert existing leases.

Existing parent symlinks and `..` are resolved while missing leaves are preserved. Names are not universally NFC-normalized. Existing case, Unicode and hardlink aliases are compared through filesystem identity. `*` cannot cross `/`; `**` spans directories. `/a/b` and `/a/bb` remain separate. Ambiguous wildcard/wildcard intersections conservatively conflict. Wildcard directory symlinks are resolved, including future files under their targets. Scope resolution reads metadata only and stops at 1,000 steps/0.25 seconds; comparison stops at 10,000 pairs/0.25 seconds. `GLOB_SCOPE_UNKNOWN` rejects the request instead of silently treating a broad reservation as nonconflicting.

Parent traversal after a wildcard, unknown symlink anchors and ambiguous separators are rejected. `tool://`, `resource://` and `service://` are separate exact-match virtual types. Tilde expansion and Windows drive mapping are never guessed. A client must supply a verified WSL mapping into the filesystem visible to the local Mail server. Remote hosts with separate filesystems are outside this contract.

The CLI only normalizes; it does not contact Mail or acquire reservations:

```sh
python -m agentstack_mail.reservation_clients --cwd /workspace/repo 'src/new file.py'
python -m agentstack_mail.reservation_clients --wsl-drive c=/mnt/c 'C:\workspace\new.py'
```

## Candidate routes

ASCII case aliases for missing leaves are recognized only after proving an existing directory alias on the same volume. Leaf spelling is retained; case-sensitive volumes keep distinct names. Unicode casefold/NFC is never applied universally.

Unicode rules use read-only native metadata for the target directory's filesystem. Modern APFS canonical equivalence and HFS+ Unicode 3.2 with exclusions apply consistently to missing leaves, glob matching and comparison, without changing saved spelling. Known byte-distinguishing Linux filesystems require verification of directory casefold flags, retaining distinct NFC/NFD files. Unknown mounts/APIs/Unicode versions reject acquisition with `UNICODE_RULES_UNKNOWN` (Linux casefold directories use `FILESYSTEM_RULES_UNKNOWN`). Runtime detection never creates witness files. See [the APFS naming contract](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/APFS_Guide/FAQ/FAQ.html) and [HFS+ Unicode rules](https://developer.apple.com/library/archive/technotes/tn/tn1150.html).


On case-insensitive filesystems, candidate acquisition rejects any non-ASCII path, including directory names, with `CASE_RULES_UNKNOWN`. Proving an ASCII directory alias does not establish the filesystem's versioned non-ASCII case table; Python casefold is not applied universally. Non-ASCII globs and ASCII globs encountering unsupported non-ASCII entries refuse uncertain acquisition/coverage. Unicode canonical equivalence and distinct names remain supported on case-sensitive filesystems. This restriction lasts until a pinned filesystem case table is implemented; default legacy operation is unaffected.

`ReservationClient` requires an authenticated binding resolver and candidate dispatch, sharing normalization across acquire/check/renew/release. The existing proxy's explicit `candidate_reservations()` factory uses `_resolve()` on each call and requires observed cwd and backend injection. It is not connected to default `tools/call`; the legacy proxy still requires relative paths.

`hook_guard()` and `hook_release()` use that client. Confirmed nonparticipants are noops. Bound participants require reservations in any folder; unknown binding, authentication failure and transport errors block. The adapters never register identities or search for credentials. The shell's explicit `reservation_absolute_paths()` helper uses the same CLI with observed cwd; existing hooks do not call it. Sandbox, OS access, Codex add-dir and browsing permissions remain unchanged.

Release retains the installed hook's failed-result boundary. Errors, success:false, failed/blocked status and error strings in tool_result/tool_response/tool_output, including nested results, are noops without binding resolution or release dispatch. Successful edits release; failed edits keep their reservation for retry.

Batch acquire rejects the whole batch on any conflict. Shared leases coexist; either exclusive lease conflicts. Checks require concrete files. Renew/release enforce ownership and select identical normalized patterns, with ID XOR path selectors. Omitting selectors selects only ACTIVE leases of that owner; an explicit empty path list is rejected. TTL/extensions range from 60 to 86,400 seconds. Renew cannot resurrect expired leases.

## Expiration and release

S2c derives ACTIVE from unreleased rows whose expiry is in the future. Expiration never rewrites history, release timestamps or revisions. There are no activity probes, Git activity checks, grace periods or collectors. Renewal adds seconds to existing expiry without resurrecting expired rows. Explicit owner release, ACTIVE-only retirement and operator force change state. See [S2c](global-reservations.en.md) for the complete contract, legacy differences and test migration.

The legacy app's independent activity/liveness implementation and 57-path performance gate remain. Runtime client/hook wiring belongs to 4b/4c; activation belongs to PR7.
