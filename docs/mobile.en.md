# Read Telemetry on your phone

> 日本語: [mobile.md](mobile.md)

[README](../README.en.md) · [Installation](install.en.md)

The mobile reader shows agents needing attention, a Mail timeline and NETWORK in English with a dark theme. **It is off by default.** Normal installation and updates start neither a listener nor Tailscale Serve. It reads legacy Telemetry on the same computer, using API 30 or later. Global mode is not supported.

## Ask your agent to set it up

You can hand this page to the Claude Code or Codex agent on the computer that runs Telemetry. For example:

> Read docs/mobile.en.md and set up the mobile reader for my phone. Do the commands yourself, stop at the steps marked "you", and tell me exactly what to do there.

At steps marked **you**, use the Tailscale admin console or the phone itself. The agent can handle the other steps. To use Terminal too, continue with the [separate opt-in steps](#inspect-a-pane-and-send-input-from-your-phone) after setting up viewing.

| Step | Who | What |
|---|---|---|
| 1 | agent | Confirm `curl -s http://127.0.0.1:8770/api/version` reports API 30 or later |
| 2 | **you** | Install Tailscale on the phone and sign in with the same account as the computer |
| 3 | agent | Find the values: computer Serve name and IP from `tailscale status --json` (`Self.DNSName` without the trailing dot, `Self.TailscaleIPs[0]`), owner login from `User[Self.UserID].LoginName`, phone IP from `tailscale status`. `tailscale whois <phone IP>` confirms the phone belongs to that login |
| 4 | **you** | In the admin console, DNS page: enable MagicDNS and HTTPS Certificates if they are off. `CertDomains` in `tailscale status --json` is empty until then |
| 5 | **you** | Review who can connect. Restricting access to your operating devices is recommended; multiple personal phones and tablets are fine. If you choose this restriction, adjust the grant and broader existing permissions as described below |
| 6 | agent | Run steps 2–5 of [Enable manually](#enable-manually) |
| 7 | **you** | Open `https://<Serve name>:9443/` on the phone. The first HTTPS request can take a few seconds while the certificate is issued |
| 8 | agent + **you** | Check each intended device. If you chose device restrictions, also check that excluded devices are refused (see [Verify on real devices](#verify-on-real-devices)) |

On macOS the CLI is usually `/Applications/Tailscale.app/Contents/MacOS/Tailscale` when `tailscale` is not on `PATH`. The reader runs in the foreground; to keep it after closing the terminal, run it under your own service manager or `nohup`. Telemetry does not install a resident service for it.

## Reading the screen

- **Activity** lists sender, recipient, time and To/Cc only. Tap one Mail event to open its subject and body. Tasks and private paths are not separately fetched, but anything written in a Mail body is visible. Status records changes observed after this reader starts, rather than claiming to know when each change actually happened.
- **Graph** defaults to agents with observed activity in the last 24 hours. Focus shows the selected agent and up to five strongest peers within that period; All shows every agent in that period. Use Find, pinch to zoom, drag to pan, and Fit. Large graphs do not guarantee that every name is readable at once.
- Line width and arrow size follow Mail counts in the selected period. Counts are **recipient rows** by direction and To/Cc, rather than unread counts or unique sent messages. Dashed parent–child lines are separate from Mail.
- Context arcs and working motion use the PC renderer. Unknown context has no arc, and its sampling time remains unknown. Account usage is Unavailable; this reader never refreshes it.
- The six **Tune graph** controls adjust this browser's display only. Filtering Activity also changes only the view. There are no launch, stop or Mail reply controls. Terminal viewing and input require the separate opt-in below.

The reader polls about every ten seconds in the foreground and stops polling in the background. Failed observations retain the last known values, visibly marked as such; failure is never a healthy zero or a waiting agent. Snapshot time is the HTTP observation time. Mail shows up to 200 recipient events per page; **Older / Newer** replaces the page. Graph polling continues while you read an older page, without moving it back to the latest page. Status appears on the latest page only. Graph aggregation has a separate 10,000-link limit. Truncation is shown explicitly. Only six display settings are saved in the browser: no offline snapshot storage or service worker is used.

Tap a Graph Mail edge to open metadata between those two agents in the selected period. **Both** directions is the default; the sender→recipient buttons filter the direction. Parent–child lines do not open Mail. Tap one event for its content, **Back** for the list, and **Done** for Graph. Selection and zoom are retained.

While Graph is in the foreground, a rise in Mail link counts between snapshots (about every 10 seconds) sends a light from sender to recipient with a **+N** label. To/Cc rows for the same directed pair are combined. Tap the light for the existing pair list; subjects and bodies are fetched only when you choose one message. Initial load, period changes, returning from the background and interrupted observations do not replay older activity. Links outside the displayed graph are omitted; snapshots with truncated link counts do not produce lights. At most 12 lights appear at once. This is a difference in period counts: older Mail leaving a rolling window can offset new arrivals. With reduced motion enabled, a stationary mark appears at the link midpoint instead.

Use **Expand** at the top right of Graph to fill the screen. If the browser's fullscreen API is unavailable or refused, Graph fills the page instead. Find, Focus/All, Fit, pinch, drag, Mail-pair lists and the Terminal sheet remain available. **Close** restores the normal view. Back or Escape closes the foreground sheet first, then exits the expanded Graph. If the OS or browser exits fullscreen, the normal view returns and open sheets close. The layout follows orientation changes and retains your selection and manual zoom. Fullscreen, back gestures and keyboard behavior on an actual phone require separate acceptance.

Subjects are limited to 512 characters and bodies to 64 KiB of UTF-8, with explicit truncation notices. HTML and Markdown remain literal text; links, images and attachments do not execute. No read receipt, ACK or reply is sent. Closing, choosing another message, changing the period or view, and moving to the background clear the displayed content. Late responses cannot restore it. Unavailable, deleted, changed or expired events show an error, never the previous message's body.

After an owner-authenticated snapshot request, the Mac reader prepares a fixed last 24 h in memory: up to 2000 distinct messages and 32 MiB of serialized content plus metadata. This is a logical JSON byte budget, not an RSS guarantee. Multiple To/Cc recipients share one body. Capacity, time or read failures are shown as **partial / unavailable**, rather than complete coverage. Every tap rechecks the current delivery and revision, including cache hits. Older displayed events remain readable one at a time and are not cached. Stopping the reader clears all caches. Subjects and bodies are never bulk-sent to the phone.

Page positions expire after 10 minutes, with at most 256 on the host and 2000 issued events. The browser keeps at most 20 navigation positions. Expiry produces an explicit error rather than silently returning to the latest page. Changing the period resets pages and content.

The small time control at the bottom of Graph, or beside Activity's filters, opens **Time range**. Its slider covers 15 minutes to 30 days in 15-minute increments. It starts at 24 hours; **All history** is a separate explicit choice, distinct from Graph's All mode. Changes apply about 200ms after the last input. The period filters agent activity, Mail events and counts, and the observation time of Status changes. Status retains the latest 200 changes after reader start, then shows those matching the period and displayed agents.

Activity uses canonical last-active and Mail timestamps. Recently active retired agents remain included; merely running does not turn old activity into current activity. Agents with unknown activity times are excluded from finite periods with an explicit count, and included in All history. Observation and projection have limits; the host's large all-history reads are not guaranteed to finish within the deadline.

Special agent icons use the PC's existing portrait configuration, with a scientific portrait and then initials as fallbacks. The private map and file paths are never sent to the phone. Portraits require the same owner authentication, accept bounded PNG images, and fall back to initials on fetch or decode failure.

## Restrict the account and the phone separately

Use **Tailscale Serve inside your tailnet**, with neither Funnel nor direct sharing of the full dashboard or cockpit. The relay listens only on `127.0.0.1` and checks the outer HTTPS Host and exact owner login. [Serve replaces incoming identity headers with its own user information](https://tailscale.com/docs/features/tailscale-serve). Another process on the same computer can forge these headers; this is not protection from untrusted local processes.

**Matching the account does not restrict which devices can connect.** We recommend allowing only devices you use to operate the terminal; multiple personal phones and tablets are fine. Strongly consider excluding always-on, public-facing servers on the same account: they could also reach the terminal, increasing the impact if compromised. This is a recommendation, not a product requirement to use one device, and the product does not automatically verify the policy.

If you choose this restriction, have the administrator allow your operating devices' Tailscale IPs to the dedicated HTTPS port. Merge the following example into the existing policy, remove unused devices from `src`, and replace the placeholders. Do not overwrite the whole policy with it.

```json
{
  "grants": [{
    "src": ["<PHONE_TAILSCALE_IP>", "<TABLET_TAILSCALE_IP>"],
    "dst": ["<SERVER_TAILSCALE_IP>"],
    "ip": ["tcp:9443"]
  }]
}
```

[Grants and selectors are additive](https://tailscale.com/docs/reference/syntax/grants). A broader existing allow rule covering this port must also be narrowed; adding a phone-specific grant cannot subtract access. Listing the user and phone IP together in `src` is OR, not AND. See [IP selectors](https://tailscale.com/docs/reference/targets-and-selectors). Device names and browser User-Agent strings are not authentication. Do not substitute a tag on the user's phone: tagging changes its user identity.

Review **every grant and ACL** applying from excluded devices to this host, so no other broad permission still allows `tcp:9443`. For example, the following `ip` values remove only TCP 9443 from a broad grant while allowing the other TCP ports, UDP and ICMP:

```json
["tcp:1-9442", "tcp:9444-65535", "udp:*", "icmp:*"]
```

This illustrates [protocol and port syntax](https://tailscale.com/docs/reference/syntax/grants#network-layer-capabilities). It is not a replacement policy or a universal substitute for `["*"]`. Preserve any needed permissions for other protocols and destinations, and have the administrator review the changes. The agent does not rewrite policy automatically.

## Enable manually

1. Update Telemetry to API 30 or later and confirm that its normal local dashboard has observations. Keep the existing services and provider configuration.
2. Review current access. If you choose the recommended device restriction, complete that policy change above. Save the current `tailscale serve status --json` output and confirm that the example HTTPS port `9443` is unused and is not a Funnel listener. Do not reuse a port with existing configuration.
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

This uses the [Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve) in the foreground and does not reset existing entries. Add `--bg` to keep the Serve entry after the terminal closes; remove it with the command in [Disable and restore](#disable-and-restore). A direct relay request without the owner header should return 403. If Host or Origin is rejected, check the actual Serve name, port and Tailscale version rather than relaxing the guard.

## Verify on real devices

Open `https://computer.example.ts.net:9443/` on each intended phone or tablet and confirm that the owner can load the page and snapshot. Also check that another account is refused. If you chose device restrictions, separately verify refusal from **excluded devices on the same account**. Owner-header authentication and device policy are separate checks. Do not report access as restricted to operating devices if the excluded-device checks are incomplete.

Repository tests use isolated HTTP and synthetic screens. They do not establish acceptance of the installed Tailscale version, real policy or actual phone. The operator performs that deployment verification separately.

## Inspect a pane and send input from your phone

**Terminal is a separate opt-in. Both capture and input are off by default.** Starting the viewing reader alone does not enable it. Device restrictions are the optional recommendation above; you can allow several operating devices. The existing `--phone-policy-reviewed` declaration is still required to acknowledge that the operator reviewed current access. It does not verify policy or enforce a single phone.

You can also give this section to your agent. It should stop at your steps and preserve existing settings.

| Step | Who | What |
|---|---|---|
| 1 | agent | Confirm Telemetry API 31 or later for legacy, a matching API34 version for fresh, the ORRERY pane adapter, and the current backend's launch method and manager |
| 2 | **you** | Review access. If you chose device restrictions, check allowed operating devices and refusal from excluded ones. Agree to the temporary disconnection during restart |
| 3 | agent | Add or update only the JSON field below in the existing `~/.orrery/config.json`, preserving other fields. File mode `0600` is recommended |
| 4 | agent | Explicitly stop and restart the backend through the identified launch procedure. Reusing a running process does not apply the setting. Do not start a duplicate or use blanket kills; stop and report an unknown manager |
| 5 | agent | Use the capability check below to confirm `api:31` for legacy or `api:34` for fresh, and `enabled:true` after human opt-in. It captures no pane and sends no input |
| 6 | agent | Add `--enable-terminal --phone-policy-reviewed --cockpit-port 8791` to the reader's existing launch example. Run `--check` with the same flags first, then restart the existing reader through its current manager |
| 7 | **you** | Open Terminal on your phone or tablet, check the selected pane and display, and review the exact text before every test send |

Add this field to the shared settings; the example does not replace the whole file.

```json
{"enable_mobile_terminal": true}
```

New backends started by `scripts/start-cockpit.sh`, setup reached through `get.sh`, or ORRERY.app read the same setting. Custom backend commands can still use `--enable-mobile-terminal`. **Closing ORRERY.app leaves the backend running.** If reopening the app does not apply the setting, restart the backend through its original manager.

The following check sends no input. Replace `owner@example.com` with the reader's owner login, and change the first path for a custom installation.

```sh
python3 - "$HOME/.agentstack/dashboard" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from mobile_input import InputClient
capability = InputClient(8791, 'owner@example.com').call('capability')
print({key: capability[key] for key in ('api', 'enabled')})
raise SystemExit(0 if capability['enabled'] else 1)
PY
```

Do not treat `enabled:false` or an error as successful activation; check the version, settings and process manager. Reader `--check` validates syntax, not policy or device acceptance. Missing opt-ins, incompatible versions, existing target protection or unavailable OS identity checks leave Terminal unavailable.

The header says **Terminal enabled** when available. The selected Graph agent panel, Activity Status rows, attention rows and Mail-detail From/To entries have explicit **Terminal** buttons leading to the same sheet. Existing Graph selection and Mail-detail actions remain. Choose a pane when several exist; an unknown mapping is never guessed from the label. In legacy, only panes already handled by the PC manager on the same transport and admitted by its protection are eligible. Agent type and the displayed time period are not authorization. No other socket or global target is used as a fallback. A pane's session may be outside the current metadata project. For fresh, use the explicit I/A procedure below.

The upper area shows the selected pane's last 40 lines, limited to 6 KiB of UTF-8. Only that open, foreground pane updates about every three seconds; failures back off to 6, 12 and at most 24 seconds. Removed control sequences and shortened text are marked. Images, HTML and links never execute. Full recorder/restored history is not forwarded. **Terminal text can contain keys or private writing.** Check it before sharing the screen.

Write one line of at most 4 KiB UTF-8 below it. Use **Review input** to check the pane, exact text and Enter, then **Send once**. Confirmation expires after 30 seconds. Output refreshes do not replace its text, target or expiry. A changed pane or failed preview requires a new review. Keep the sheet open in the foreground to see the pane's response after sending. **Received by terminal** means tmux accepted the batch, not that an agent ran or completed the task. **Outcome unknown** can mean that only part arrived; there is no automatic retry. **Check receipt** only queries its receipt.

This is a terminal operation you confirm by inspecting the pane. Changes of program or a return to a shell within the same pane are not guaranteed to be detected. Text may be interpreted by that shell, combined with existing input or another writer, or sent after the screen changes. The feature does not clear input buffers or interrupt the pane before sending.

Close, pane changes, backgrounding and leaving stop polling and clear the display, draft and unsent confirmation. Late responses cannot restore them. Content is not placed in URLs, logs, browser storage or a server preview cache. This does not erase existing PC recorder output or OS recording/screenshots. Actual Android keyboard behavior, Serve/phone policy and real panes need separate acceptance; isolated HTTP/CDP checks do not establish them.

To disable only Terminal, set the shared field to `false` or remove it, also remove any explicit `--enable-mobile-terminal` from custom backend arguments, and remove those three reader flags. Restart both processes through their existing procedures; leaving them running does not disable the feature. Viewing settings and unrelated Serve entries can remain.

## Disable and restore

Remove only this Serve listener, then press Ctrl-C in the reader's terminal.

```sh
tailscale serve --https=9443 off
```

If you used additional flags, [repeat the original flags with off](https://tailscale.com/docs/reference/tailscale-cli/serve). Do not use `serve reset`, which removes unrelated entries. Compare the remaining Serve configuration with the saved output and confirm that only the dedicated entry was removed. If a policy rollback is needed, have the administrator revert only this change. Stopping the reader clears its in-memory snapshot and observed status history; it does not stop Telemetry or Mail.

See the [API contract](api.en.md#mobile-reader-api-30) for authentication, routes and projected fields.

### Explicit fresh use

Follow the [fresh guide](fresh-global-update.en.md) to verify the same host/UID/context and new I. Start the dashboard before the matching Cockpit backend. Ordinary service/Finder startup uses the shared loader for saved fresh references, while real-environment startup/display acceptance remains separate; API34 alone does not authorize production cutover. Human opt-in stays off by default.

Fresh resolves the selected I/A to its current native/socket/pane and uses only the same bridge already managed by the PC manager. It never falls back through a display name, last pane or old project/DB. Choice, preview, confirmation, one send and status retain the same I/A and target. Preview waits at most three seconds and ACK five seconds; timeout/unknown is not automatically resent. The normal 30-second confirmation, 4 KiB body limit and read/ACK separation remain. Real phone/provider/trust and installation acceptance are separate.
