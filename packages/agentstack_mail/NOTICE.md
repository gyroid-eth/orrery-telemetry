# Source provenance

This package contains portions derived from MCP Agent Mail:

- repository: `https://github.com/Dicklesworthstone/mcp_agent_mail`
- original copyright: Copyright (c) 2026 Jeffrey Emanuel
- live source checkout HEAD: `ad0e4788967d809979fa25004cf52545fdcd888a`
- tracked live working-tree diff SHA-256:
  `8f592e415af1cb00c8daea9b190fadf8f9dcfbaa6d4b2b957c8a690da05f9eac`
- tracked `src/mcp_agent_mail` and `scripts` diff SHA-256:
  `bab9363ffc9bddb61a5076934bf2ee042f17a224305b5b932a6f37ae098ab7bd`
- captured live `tools/list` fixture SHA-256:
  `6ea7dabf41f71091161fa1fcb8a4073a383a65c7bba4785306217fd35f9e8332`
- self-contained live Git bundle SHA-256:
  `55f03ea48a3279f090c4b93436af1d55f912c75c3f985e4ba06a8b95d39f7670`

The live checkout is a shallow history with five local commits above its
shallow boundary plus uncommitted changes. It, not current upstream HEAD, is
the behavior baseline for the extraction. Current upstream is an advisory
source for individually reviewed security and bug fixes, not a merge target.
The repository preserves a complete-history Git bundle through live HEAD and
the tracked dirty patch under `provenance/`; its README gives the verified
reconstruction procedure.

Implemented in the core extraction:

- distribution and import namespace renamed to `agentstack-mail` and
  `agentstack_mail`;
- MCP server key, service labels, environment namespace, port, database,
  archive, and signal roots isolated from AgentMail defaults;
- only AgentStack's audited 22-tool compatibility surface is published;
- live-derived tool bodies remain temporarily internal until differential
  tests permit removal of the non-compatibility bodies;
- machine-specific daemons, HTTP UI, and presentation policy are excluded;
- model normalization remains temporarily because it affects compatibility
  responses from registration, whois, and session macros;
- live per-message notification and runtime-stability fixes preserved with new
  acceptance tests.

The initial derived module snapshot comprises `app.py`, `config.py`, `db.py`,
`models.py`, `storage.py`, `utils.py`, `rich_logger.py`,
`model_normalize.py`, `guard.py`, and `llm.py`. Changes made during the initial
copy are limited to provenance pointers, the Python namespace rename, isolated
configuration/defaults, fail-closed tool/resource publication, and lazy loading
of the temporarily retained non-compatibility LLM helper. Behavioral changes to
the 22 compatibility tools require a later differential-test manifest.

License boundaries are per component:

- new AgentStack-authored files are governed by `AGENTSTACK_LICENSE`
  (PolyForm Perimeter 1.0.1);
- copied or semantically derived AgentMail portions retain their original
  copyright and are governed by `UPSTREAM_LICENSE`;
- a file containing both kinds of work must retain both notices.

The distribution metadata declares both license families because the package
contains multiple components; it does not relicense either component. This
notice is provenance documentation, not legal advice.
