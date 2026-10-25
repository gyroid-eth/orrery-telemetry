# Read Telemetry on your phone

> 日本語: [mobile.md](mobile.md)

[README](../README.en.md) · [Installation](install.en.md)

The mobile reader shows agents needing attention, a Mail timeline and NETWORK in English with a dark theme. **It is off by default.** Normal installation and updates start neither a listener nor Tailscale Serve. It reads legacy Telemetry on the same computer, using API 29 or later. Global mode is not supported.

## Reading the screen

- **Activity** shows sender, recipient, time and To/Cc. Subjects, bodies, tasks and private paths are excluded. Status records changes observed after this reader starts, rather than claiming to know when each change actually happened.
- **Graph** defaults to agents with observed activity in the last 24 hours. Focus shows the selected agent and up to five strongest peers within that period; All shows every agent in that period. Use Find, pinch to zoom, drag to pan, and Fit. Large graphs do not guarantee that every name is readable at once.
- Line width and arrow size follow Mail counts in the selected period. Counts are **recipient rows** by direction and To/Cc, rather than unread counts or unique sent messages. Dashed parent–child lines are separate from Mail.
- Context arcs and working motion use the PC renderer. Unknown context has no arc, and its sampling time remains unknown. Account usage is Unavailable; this reader never refreshes it.
- The six **Tune graph** controls adjust this browser's display only. Filtering Activity also changes only the view. There are no launch, stop, reply or terminal controls.

The reader polls about every ten seconds in the foreground and stops polling in the background. Failed observations retain the last known values, visibly marked as such; failure is never a healthy zero or a waiting agent. Snapshot time is the HTTP observation time. Mail shows at most the latest 200 recipient events; graph aggregation has a separate 10,000-link limit. Truncation is shown explicitly. Only six display settings are saved in the browser: no offline snapshot storage or service worker is used.

The small time control at the bottom of Graph, or beside Activity's filters, opens **Time range**. Its slider covers 15 minutes to 30 days in 15-minute increments. It starts at 24 hours; **All history** is a separate explicit choice, distinct from Graph's All mode. Changes apply about 200ms after the last input. The period filters agent activity, Mail events and counts, and the observation time of Status changes. Status retains the latest 200 changes after reader start, then shows those matching the period and displayed agents.

Activity uses canonical last-active and Mail timestamps. Recently active retired agents remain included; merely running does not turn old activity into current activity. Agents with unknown activity times are excluded from finite periods with an explicit count, and included in All history. Observation and projection have limits; the host's large all-history reads are not guaranteed to finish within the deadline.

Special agent icons use the PC's existing portrait configuration, with a scientific portrait and then initials as fallbacks. The private map and file paths are never sent to the phone. Portraits require the same owner authentication, accept bounded PNG images, and fall back to initials on fetch or decode failure.

## Restrict the account and the phone separately

Use **Tailscale Serve inside your tailnet**, with neither Funnel nor direct sharing of the full dashboard or cockpit. The relay listens only on `127.0.0.1` and checks the outer HTTPS Host and exact owner login. [Serve replaces incoming identity headers with its own user information](https://tailscale.com/docs/features/tailscale-serve). Another process on the same computer can forge these headers; this is not protection from untrusted local processes.

**Matching the account does not restrict access to one phone.** Have the tailnet administrator allow only the designated phone's Tailscale IP to a dedicated HTTPS port. This example belongs in the existing policy; replace placeholders and do not overwrite the whole policy with it.

```json
{
  "grants": [{
    "src": ["<PHONE_TAILSCALE_IP>"],
    "dst": ["<SERVER_TAILSCALE_IP>"],
    "ip": ["tcp:9443"]
  }]
}
```

[Grants and selectors are additive](https://tailscale.com/docs/reference/syntax/grants). A broader existing allow rule covering this port must also be narrowed; adding a phone-specific grant cannot subtract access. Listing the user and phone IP together in `src` is OR, not AND. See [IP selectors](https://tailscale.com/docs/reference/targets-and-selectors). Device names and browser User-Agent strings are not authentication. Do not substitute a tag on the user's phone: tagging changes its user identity.

## Enable manually

1. Update Telemetry to API 29 or later and confirm that its normal local dashboard has observations. Keep the existing services and provider configuration.
2. Check the phone's effective policy. Save the current `tailscale serve status --json` output and confirm that the example HTTPS port `9443` is unused and is not a Funnel listener. Do not reuse a port with existing configuration.
3. Check the options below. Replace `owner@example.com` with the exact Serve login and `computer.example.ts.net` with this computer's actual Serve name. Omit a trailing slash from the origin. Replace the script path for a custom installation. Python 3.11 or later is required.

```sh
python3 "$HOME/.agentstack/dashboard/mobile_relay.py" --check \
  --owner-login 'owner@example.com' \
  --public-origin 'https://computer.example.ts.net:9443' \
  --listen-port 8781 --upstream-port 8770
```

`--check` validates option syntax only. It makes no connections, starts no listener and changes no policy. It does not verify the phone restriction. Resolve missing options before continuing.

4. Keep the reader running in one terminal.

```sh
python3 "$HOME/.agentstack/dashboard/mobile_relay.py" --enable \
  --owner-login 'owner@example.com' \
  --public-origin 'https://computer.example.ts.net:9443' \
  --listen-port 8781 --upstream-port 8770
```

5. In another terminal, connect the dedicated Serve port to this reader. Do not point it at 8770 or cockpit.

```sh
tailscale serve --https=9443 http://127.0.0.1:8781
```

This uses the [Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve) in the foreground and does not reset existing entries. A direct relay request without the owner header should return 403. If Host or Origin is rejected, check the actual Serve name, port and Tailscale version rather than relaxing the guard.

## Verify on real devices

Open `https://computer.example.ts.net:9443/` on the designated phone and confirm that both the page and snapshot load for the owner. Confirm rejection from **another device on the same account** and from another account. Verify both owner-header admission and effective device policy. Until all of these checks pass, do not describe the deployment as restricted to the owner's phone.

Repository tests use isolated HTTP and synthetic screens. They do not establish acceptance of the installed Tailscale version, real policy or actual phone. The operator performs that deployment verification separately.

## Disable and restore

Remove only this Serve listener, then press Ctrl-C in the reader's terminal.

```sh
tailscale serve --https=9443 off
```

If you used additional flags, [repeat the original flags with off](https://tailscale.com/docs/reference/tailscale-cli/serve). Do not use `serve reset`, which removes unrelated entries. Compare the remaining Serve configuration with the saved output and confirm that only the dedicated entry was removed. If a policy rollback is needed, have the administrator revert only this change. Stopping the reader clears its in-memory snapshot and observed status history; it does not stop Telemetry or Mail.

See the [API contract](api.en.md#mobile-reader-api-29) for authentication, routes and projected fields.
