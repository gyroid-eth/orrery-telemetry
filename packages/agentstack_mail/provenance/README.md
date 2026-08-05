# Reconstructing the tracked live baseline

This directory makes the authoritative tracked Python AgentMail source
reconstructible without relying on the mutable checkout at
`/Users/operator/mcp_agent_mail`.

| Artifact | SHA-256 | Purpose |
| --- | --- | --- |
| `live-head.bundle` | `55f03ea48a3279f090c4b93436af1d55f912c75c3f985e4ba06a8b95d39f7670` | Complete history through live HEAD `ad0e4788967d809979fa25004cf52545fdcd888a`, including the five local commits |
| `working-tree-tracked.patch` | `8f592e415af1cb00c8daea9b190fadf8f9dcfbaa6d4b2b957c8a690da05f9eac` | All tracked dirty changes above that HEAD |

To reconstruct the tracked tree in a new directory:

```bash
AGENTSTACK_REPO=/absolute/path/to/claude-agent-stack
git clone "$AGENTSTACK_REPO/packages/agentstack_mail/provenance/live-head.bundle" /tmp/agentmail-live
git -C /tmp/agentmail-live apply "$AGENTSTACK_REPO/packages/agentstack_mail/provenance/working-tree-tracked.patch"
```

Before extraction, verify the two SHA-256 values and run `git bundle verify`.
The bundle reconstructs the tracked HEAD tree and a complete, fsck-clean
history. It was produced by combining the live commits with the full reference
history before bundling.
The original untracked backup and diagnostic harness files are deliberately
not product-source inputs; their audited copies are retained in the development
vault baseline.
