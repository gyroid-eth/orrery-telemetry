# Source provenance

This package contains or will contain portions derived from MCP Agent Mail:

- repository: `https://github.com/Dicklesworthstone/mcp_agent_mail`
- original copyright: Copyright (c) 2026 Jeffrey Emanuel
- live source checkout HEAD: `b8251c1336e5fdca80a91b8b608d843df91b64e8`
- tracked live working-tree diff SHA-256:
  `8f592e415af1cb00c8daea9b190fadf8f9dcfbaa6d4b2b957c8a690da05f9eac`
- tracked `src/mcp_agent_mail` and `scripts` diff SHA-256:
  `bab9363ffc9bddb61a5076934bf2ee042f17a224305b5b932a6f37ae098ab7bd`
- captured live `tools/list` fixture SHA-256:
  `6ea7dabf41f71091161fa1fcb8a4073a383a65c7bba4785306217fd35f9e8332`
- self-contained live Git bundle SHA-256:
  `2265572de9ae1161c0be5e2681137d10205400cc01c3efe93bbcb16c30e37a1e`

The live checkout has five behavior-bearing local commits above its shallow
boundary, followed by the security-only signing-key deletion at the recorded
HEAD, plus uncommitted changes. It, not current upstream HEAD, is the behavior
baseline for the extraction. Current upstream is an advisory source for
individually reviewed security and bug fixes, not a merge target.
The repository preserves a depth-1 Git bundle of the live HEAD tree and the
tracked dirty patch under `provenance/`; its README gives the verified
reconstruction procedure. Full history is deliberately excluded so the
deleted signing-key blob is physically absent from the bundle.

Planned changes include:

- distribution and import namespace renamed to `agentstack-mail` and
  `agentstack_mail`;
- MCP server key, service labels, environment namespace, port, database,
  archive, and signal roots isolated from AgentMail defaults;
- only AgentStack's audited compatibility surface and its transitive runtime
  dependencies retained;
- machine-specific daemons and model-display code that AgentStack already owns
  excluded;
- live per-message notification and runtime-stability fixes preserved with new
  acceptance tests.

License boundaries are per component:

- new AgentStack-authored files are governed by `AGENTSTACK_LICENSE`
  (PolyForm Perimeter 1.0.1);
- copied or semantically derived AgentMail portions retain their original
  copyright and are governed by `UPSTREAM_LICENSE`;
- a file containing both kinds of work must retain both notices.

The distribution metadata declares both license families because the package
contains multiple components; it does not relicense either component. This
notice is provenance documentation, not legal advice.
