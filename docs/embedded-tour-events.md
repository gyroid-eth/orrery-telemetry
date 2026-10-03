# Embedded interaction notifications

An embedded dashboard notifies its same-origin host after supported user
interactions succeed. Standalone pages send no notifications. These messages
let a host checklist follow real interactions without observing clicks or
intercepting fetch calls.

```js
{type: 'orrery-tour-action', version: 1, action: 'edge'}
```

The target origin is `location.origin`. Hosts must check both `event.origin`
and `event.source` against the iframe they own, validate the version and action,
and decide which active checklist step to complete. The message carries no
agent tasks, Mail bodies, credentials, or commands. It never requests an action.

| Action | Completion boundary |
| --- | --- |
| `exit` | A Deck card's confirmed EXIT receives HTTP success, `ok: true`, and `exit-sent`. Arming, failures and shell cleanup do not count. |
| `edge` | The selected edge's thread loads successfully and renders; stale responses and errors do not count. |
| `select` | A user node selection or completed rectangle contains at least two registered nodes. A cancelled rectangle does not notify. |
| `replay` | History loads successfully, contains events, and Replay starts. |
| `resume` | An embedded detail-panel or bulk resume receives HTTP success and the server action `resumed`. Opening an already-running agent does not count. |
| `settings` | A network slider changes its value and the synchronous rendering/application completes. Unchanged values and thrown application errors do not notify. |
| `return` | A successful embedded OPEN/RESUME is about to hand the agent to the host via the existing `orrery-jump` message. The host must finish its own successful focus/navigation before marking a return step. |

The host remains responsible for checklist order, progress persistence, and
showing instructions. This is a browser message protocol (version 1); REST
endpoints and the `/api/version` generation are unchanged.
