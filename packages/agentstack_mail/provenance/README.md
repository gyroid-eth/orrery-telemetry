# Reconstructing the tracked live baseline

This directory makes the authoritative tracked Python AgentMail source
reconstructible without relying on the mutable checkout at
`/Users/operator/mcp_agent_mail`.

| Artifact | SHA-256 | Purpose |
| --- | --- | --- |
| `live-head.bundle` | `2265572de9ae1161c0be5e2681137d10205400cc01c3efe93bbcb16c30e37a1e` | Depth-1 snapshot of live HEAD `b8251c1336e5fdca80a91b8b608d843df91b64e8` |
| `working-tree-tracked.patch` | `8f592e415af1cb00c8daea9b190fadf8f9dcfbaa6d4b2b957c8a690da05f9eac` | All tracked dirty changes above that HEAD |

To reconstruct the tracked tree in a new directory:

```bash
AGENTSTACK_REPO=/absolute/path/to/claude-agent-stack
git clone "$AGENTSTACK_REPO/packages/agentstack_mail/provenance/live-head.bundle" /tmp/agentmail-live
git -C /tmp/agentmail-live rev-parse HEAD > /tmp/agentmail-live/.git/shallow
git -C /tmp/agentmail-live apply "$AGENTSTACK_REPO/packages/agentstack_mail/provenance/working-tree-tracked.patch"
```

Before extraction, verify the two SHA-256 values and run `git bundle verify`.
The bundle intentionally contains only the tracked HEAD snapshot. Mark the tip
as the shallow boundary before history traversal or `git fsck`; do not replace
it with a full-history bundle, which would restore the deleted private-key blob.
The original untracked backup and diagnostic harness files are deliberately
not product-source inputs; their audited copies are retained in the development
vault baseline.
