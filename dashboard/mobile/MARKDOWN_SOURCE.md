# Bundled Markdown renderer

`conversation_markdown.js` is from `gyroid-eth/orrery`,
`bridge/conversation_markdown.js` at commit `d12a266`, under the
PolyForm Perimeter License 1.0.1 (Copyright © 2026 gyroid).
The first line records provenance; every byte after that line is unchanged.
Mobile styling and integration belong in `mobile.css` and `conversation.js`.
Do not edit the bundled parser independently.

To update, replace the body from the upstream file, update its commit in the
first line and here, then update the pinned SHA-256 in
`tests/test_mobile_markdown_source.py`. Run the source comparison with
`ORRERY_MARKDOWN_SOURCE` pointing to the upstream `bridge/conversation_markdown.js`:

```sh
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_mobile_markdown_source.py
```

The checksum always runs. The byte comparison skips if no upstream source is
supplied. Both run without a network request or an upstream checkout dependency.
The mobile Reader decorates the renderer's `span.markdown-image` markers.
Local raster images use only preview IDs issued for their conversation event.
HTTP(S) image targets become links, while ordinary local path candidates keep
the renderer's inert text behavior.
