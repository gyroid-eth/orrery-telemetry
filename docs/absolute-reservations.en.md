# Absolute reservation candidate (PR2)

[日本語](absolute-reservations.md)

## Scope

PR2 for #213 provides a shared normalizer, an authenticated candidate server, an explicit bound-proxy factory, hook guard/release adapters, a normalization CLI, and per-lease activity/GC. Published Mail tools, database schema, default proxy calls, installation settings and existing hooks remain in legacy mode. There is no environment switch. Merging this PR alone does not change operation or the dashboard API generation.

`ReservationServer` is an explicitly constructed, isolated in-memory store with a mandatory authentication callback. Callers cannot choose owner IDs. Persistent storage, proven legacy lease imports, instance/binding cutover and connection to the real hook session resolver belong to later PRs. Do not use this candidate store as a production server.

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

Unicode rules use read-only native metadata for the target directory's filesystem. Modern APFS canonical equivalence and HFS+ Unicode 3.2 with exclusions apply consistently to missing leaves, glob matching and activity expansion, without changing saved spelling. Known byte-distinguishing Linux filesystems require verification of directory casefold flags, retaining distinct NFC/NFD files. Unknown mounts/APIs/Unicode versions reject acquisition with `UNICODE_RULES_UNKNOWN` (Linux casefold directories use `FILESYSTEM_RULES_UNKNOWN`); probes remain unknown and cannot prove stale. Runtime detection never creates witness files. See [the APFS naming contract](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/APFS_Guide/FAQ/FAQ.html) and [HFS+ Unicode rules](https://developer.apple.com/library/archive/technotes/tn/tn1150.html).

`ReservationClient` requires an authenticated binding resolver and candidate dispatch, sharing normalization across acquire/check/renew/release. The existing proxy's explicit `candidate_reservations()` factory uses `_resolve()` on each call and requires observed cwd and backend injection. It is not connected to default `tools/call`; the legacy proxy still requires relative paths.

`hook_guard()` and `hook_release()` use that client. Confirmed nonparticipants are noops. Bound participants require reservations in any folder; unknown binding, authentication failure and transport errors block. The adapters never register identities or search for credentials. The shell's explicit `reservation_absolute_paths()` helper uses the same CLI with observed cwd; existing hooks do not call it. Sandbox, OS access, Codex add-dir and browsing permissions remain unchanged.

Release retains the installed hook's failed-result boundary. Errors, success:false, failed/blocked status and error strings in tool_result/tool_response/tool_output, including nested results, are noops without binding resolution or release dispatch. Successful edits release; failed edits keep their reservation for retry.

Batch acquire rejects the whole batch on any conflict. Shared leases coexist; either exclusive lease conflicts. Checks require concrete files. Renew/release enforce ownership and select identical normalized patterns, with ID/path selectors combined by union. Omitting selectors selects all leases of that owner; an explicit empty path list is rejected. TTL/extensions range from 60 to 86,400 seconds. Renew cannot resurrect expired leases.

## Activity and collection

Renew adds its extension to the current active expiration time. A no-op renew does not update agent activity.

Each probe resolves its own prefix, actual targets, actual Git repositories and repository-relative pathspecs. Multi-repository patterns and symlink targets are probed separately. Only metadata and Git timestamps are read, never file contents or the Mail database. Empty/new paths still verify the anchor and look for recent deletion commits. A probe is bounded by 3 seconds/1,000 steps, and Git receives the remaining timeout. Permission failures, timeouts, caps, changed anchors and unknown repository/Git errors produce `activity_unknown`. Recent file/Git/Mail/agent activity and initial-write grace are retained. Unknown does not prove inactivity. Virtual namespaces have no filesystem probe.

Internal `collect()` accepts at most 100 lease IDs per batch, probes stale candidates again, and rechecks lease revision plus agent/Mail activity generation before committing. Renew/Mail changes during probes and filesystem activity in the final probe prevent early release. It cannot transactionally exclude an OS write after the final metadata check. TTL expiration and owner release are independent of unknown, which never extends TTL.

## Validation and remaining work

Temporary-HOME fixtures cover separate cwd, identical files, symlinks, missing files, globs, Unicode/case, spaces, WSL, authentication/owner/TTL, separate repository activity, empty globs, deletion commits, unknown probes, concurrent activity changes, unbound/broken hook bindings and legacy proxy behavior. Operational activation follows persistent migration verification, writer gates and real hook/binding integration in later PRs.
