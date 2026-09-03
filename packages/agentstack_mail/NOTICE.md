# Source provenance

This package contains portions derived from MCP Agent Mail:

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
The repository preserved a depth-1 Git bundle of the live HEAD tree under
`provenance/` until publication; the tracked dirty patch and the README's
audit trail remain, though the reconstruction it described can no longer be
performed here. The bundle itself was removed from this
repository and from its history before the project was made public.

Excluding full history had been the safeguard against shipping a deleted
signing-key blob, and the bundle was believed to hold only the HEAD tree. It
did not: an adversarial pass before publication found 720 commits inside it,
along with that key and the previous author identity. Nothing in this
repository's text could see them, because a bundle is compressed Git objects
and a text search reads straight past it. The lesson is recorded here rather
than in a commit message: an archive committed as a file is a second
repository, and it has to be opened, not grepped.

Implemented in the core extraction:

- distribution and import namespace renamed to `agentstack-mail` and
  `agentstack_mail`;
- MCP provider identity and both client keys use `orrery-mail`; service labels,
  environment namespace, port, database, archive, and signal roots remain
  isolated from predecessor defaults;
- only AgentStack's audited compatibility surface is published (25 tools);
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
of the retained LLM helper used by enabled summarization. Behavioral changes to
the 24 compatibility tools require a later differential-test manifest.

License boundaries are per component:

- new AgentStack-authored files are governed by `AGENTSTACK_LICENSE`
  (PolyForm Perimeter 1.0.1);
- copied or semantically derived AgentMail portions retain their original
  copyright and are governed by `UPSTREAM_LICENSE`;
- a file containing both kinds of work must retain both notices.

The distribution metadata declares both license families because the package
contains multiple components; it does not relicense either component. This
notice is provenance documentation, not legal advice.
