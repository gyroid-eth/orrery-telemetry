#!/usr/bin/env python3
"""Generate the dashboard theme-axis source inventory.

The numbers in this inventory are outputs, never inputs.  Re-run this script
after changing dashboard/index.html and commit the record-level JSON diff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX = ROOT / "dashboard" / "index.html"
DEFAULT_OUTPUT = ROOT / "dashboard" / "theme_effect_manifest.json"

COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|(?:rgba?|hsla?)\([^)]*\)")
BLOCK_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
DECLARATION_RE = re.compile(r"([\w-]+)\s*:\s*([^;}]*)", re.DOTALL)
COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
LENGTH_RE = re.compile(r"(?<![\w.])(-?(?:\d+(?:\.\d*)?|\.\d+))(px)?")

RULES: dict[str, Any] = {
    "version": 1,
    "color_literal": COLOR_RE.pattern,
    "glow": {
        "source_properties": ["box-shadow", "text-shadow", "filter"],
        "component_unit": "comma layer for box/text shadow; drop-shadow() for filter",
        "classification_order": ["focus", "elevation", "state", "emissive"],
        "focus_selector_markers": [":focus", "::-webkit-slider-thumb"],
        "state_selector_markers": [
            "arming",
            "st-work",
            "st-ask",
            "imp-",
            ".err",
            "askbub",
            "qbub",
            ".armed",
            ".arm",
            ".run",
            "retire",
            ".exit",
            "mail_sent",
            "mail_recv",
            ".spawn",
            "urgent",
            "high",
        ],
        "mutable_categories": ["emissive", "state"],
        "mutable_geometry": "zero x/y, positive blur, zero spread",
        "colored_channel_range_min": 24,
        "radial": "each saturated radial-gradient layer is an emissive component; neutral surface/vignette layers are elevation",
    },
    "small_text": {
        "source_threshold": "authored numeric px < 12",
        "runtime_threshold": "computed font-size < 12px on an owned text element",
        "excluded_selector_markers": [
            ".brand",
            "svg",
            "canvas",
            ".xterm",
            "icon",
            "glyph",
        ],
    },
    "tracking": {
        "audit_threshold": "authored numeric px >= 1.5",
        "runtime_threshold": "computed letter-spacing / computed font-size > 0.08",
        "excluded_selector_markers": [".brand", "code", "pre", ".xterm", "canvas"],
    },
    "token_targets": {
        "dim-contrast": ["--ink-dim", "--ink-faint"],
        "background": [
            "--bg",
            "--panel",
            "--panel-2",
            "--elev",
            "--ink",
            "--ink-dim",
            "--ink-faint",
            "--hair",
            "--hair-2",
        ],
    },
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _line_number(source: str, offset: int, first_line: int) -> int:
    return source.count("\n", 0, offset) + first_line


def _selector(raw: str, css_before: str) -> str:
    selector = COMMENT_RE.sub("", raw).strip().split("}")[-1].strip()
    keyframe_step = re.fullmatch(
        r"(?:from|to|\d+(?:\.\d+)?%)(?:\s*,\s*(?:from|to|\d+(?:\.\d+)?%))*",
        selector,
    )
    if keyframe_step:
        keyframes = list(re.finditer(r"@keyframes\s+([\w-]+)\s*\{", css_before))
        if keyframes:
            selector = f"@keyframes {keyframes[-1].group(1)} > {selector}"
    return " ".join(selector.split())


def _split_layers(value: str) -> list[str]:
    layers: list[str] = []
    start = depth = 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            layers.append(value[start:index].strip())
            start = index + 1
    layers.append(value[start:].strip())
    return [layer for layer in layers if layer]


def _filter_layers(value: str) -> list[str]:
    layers: list[str] = []
    needle = "drop-shadow("
    cursor = 0
    while True:
        start = value.find(needle, cursor)
        if start < 0:
            break
        depth = 1
        index = start + len(needle)
        while index < len(value) and depth:
            if value[index] == "(":
                depth += 1
            elif value[index] == ")":
                depth -= 1
            index += 1
        layers.append(value[start:index])
        cursor = index
    return layers


def _geometry(component: str) -> dict[str, float]:
    without_colors = COLOR_RE.sub("", component)
    values = [float(match.group(1)) for match in LENGTH_RE.finditer(without_colors)]
    values.extend([0.0] * (4 - len(values)))
    return {"x": values[0], "y": values[1], "blur": values[2], "spread": values[3]}


def _neutral_literal(literal: str) -> bool:
    parsed = literal.lstrip("#") if literal.startswith("#") else None
    if parsed is not None:
        if len(parsed) in {3, 4}:
            parsed = "".join(channel * 2 for channel in parsed)
        if len(parsed) not in {6, 8}:
            return True
        channels = [int(parsed[index : index + 2], 16) for index in (0, 2, 4)]
        return max(channels) - min(channels) < RULES["glow"]["colored_channel_range_min"]
    match = re.match(
        r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)", literal
    )
    if not match:
        return True
    channels = [float(channel) for channel in match.groups()]
    return max(channels) - min(channels) < RULES["glow"]["colored_channel_range_min"]


def _classify(selector: str, component: str, geometry: dict[str, float]) -> str:
    lowered = selector.lower()
    focus_markers = RULES["glow"]["focus_selector_markers"]
    state_markers = RULES["glow"]["state_selector_markers"]
    if any(marker in lowered for marker in focus_markers):
        return "focus"
    if geometry["x"] or geometry["y"] or geometry["spread"]:
        return "elevation"
    literals = COLOR_RE.findall(component)
    if literals and all(_neutral_literal(literal) for literal in literals):
        return "elevation"
    if any(marker in lowered for marker in state_markers):
        return "state"
    return "emissive"


def _component_records(selector: str, prop: str, value: str) -> list[dict[str, Any]]:
    layers = _filter_layers(value) if prop == "filter" else _split_layers(value)
    records: list[dict[str, Any]] = []
    for layer_index, component in enumerate(layers):
        literals = COLOR_RE.findall(component)
        if not literals:
            continue
        geometry = _geometry(component)
        category = _classify(selector, component, geometry)
        mutable = (
            category in RULES["glow"]["mutable_categories"]
            and geometry["x"] == 0
            and geometry["y"] == 0
            and geometry["blur"] > 0
            and geometry["spread"] == 0
        )
        records.append(
            {
                "layer": layer_index,
                "value": " ".join(component.split()),
                "color_literals": literals,
                "geometry": geometry,
                "category": category,
                "mutable": mutable,
            }
        )
    return records


def _css_blocks(html: str) -> tuple[str, int]:
    style_tag = re.search(r'<style\s+id="agentstack-dashboard-styles"\s*>', html)
    if not style_tag:
        raise ValueError("missing #agentstack-dashboard-styles source block")
    content_start = style_tag.end()
    content_end = html.index("</style>", content_start)
    first_line = html.count("\n", 0, content_start) + 1
    return html[content_start:content_end], first_line


def _declarations(css: str, first_line: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for block in BLOCK_RE.finditer(css):
        selector = _selector(block.group(1), css[: block.start()])
        for declaration in DECLARATION_RE.finditer(block.group(2)):
            prop, raw_value = declaration.groups()
            value = " ".join(raw_value.split())
            absolute_offset = block.start(2) + declaration.start()
            records.append(
                {
                    "selector": selector,
                    "property": prop,
                    "value": value,
                    "line": _line_number(css, absolute_offset, first_line),
                }
            )
    return records


def _source_effects(declarations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    properties = set(RULES["glow"]["source_properties"])
    records: list[dict[str, Any]] = []
    for declaration in declarations:
        if declaration["property"] not in properties:
            continue
        literals = COLOR_RE.findall(declaration["value"])
        if not literals:
            continue
        record = dict(declaration)
        record["color_literals"] = literals
        record["components"] = _component_records(
            record["selector"], record["property"], record["value"]
        )
        record["id"] = _digest(
            f'{record["selector"]}\0{record["property"]}\0{record["line"]}\0{record["value"]}'
        )[:16]
        records.append(record)
    return records


def _radial_effects(declarations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for declaration in declarations:
        if declaration["property"] not in {"background", "background-image"}:
            continue
        if "radial-gradient" not in declaration["value"]:
            continue
        components = []
        for layer_index, layer in enumerate(_split_layers(declaration["value"])):
            if "radial-gradient" not in layer:
                continue
            literals = COLOR_RE.findall(layer)
            colored = [literal for literal in literals if not _neutral_literal(literal)]
            if colored:
                components.append(
                    {
                        "layer": layer_index,
                        "value": " ".join(layer.split()),
                        "color_literals": colored,
                        "category": "emissive",
                        "mutable": True,
                    }
                )
        if not components:
            continue
        record = dict(declaration)
        record.update(
            {
                "components": components,
                "id": _digest(
                    f'radial\0{declaration["selector"]}\0{declaration["line"]}\0{declaration["value"]}'
                )[:16],
            }
        )
        records.append(record)
    return records


def _axis_source_records(
    declarations: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    small: list[dict[str, Any]] = []
    tracking: list[dict[str, Any]] = []
    small_excluded = RULES["small_text"]["excluded_selector_markers"]
    tracking_excluded = RULES["tracking"]["excluded_selector_markers"]
    for declaration in declarations:
        selector = declaration["selector"].lower()
        if declaration["property"] == "font-size":
            match = re.fullmatch(r"([0-9]*\.?[0-9]+)px", declaration["value"])
            if match and float(match.group(1)) < 12 and not any(
                marker in selector for marker in small_excluded
            ):
                small.append(dict(declaration))
        elif declaration["property"] == "letter-spacing":
            match = re.fullmatch(r"([0-9]*\.?[0-9]+)px", declaration["value"])
            if match and float(match.group(1)) >= 1.5 and not any(
                marker in selector for marker in tracking_excluded
            ):
                tracking.append(dict(declaration))

    return {"small-text": small, "tracking": tracking}


def generate(index: Path = DEFAULT_INDEX) -> dict[str, Any]:
    html = index.read_text(encoding="utf-8")
    css, first_line = _css_blocks(html)
    declarations = _declarations(css, first_line)
    source_effects = _source_effects(declarations)
    radial_effects = _radial_effects(declarations)
    axis_sources = _axis_source_records(declarations)
    rules_json = json.dumps(RULES, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    axes = {
        "dim-contrast": {"unit": "token-write", "records": RULES["token_targets"]["dim-contrast"]},
        "background": {"unit": "token-write", "records": RULES["token_targets"]["background"]},
        "small-text": {"unit": "source-declaration", "records": axis_sources["small-text"]},
        "tracking": {"unit": "source-declaration", "records": axis_sources["tracking"]},
        "glow": {
            "unit": "declaration",
            "records": source_effects,
            "radial_records": radial_effects,
        },
    }
    source_digest = _digest(css)
    runtime_expectations = {
        "sourceDigest": source_digest,
        "rulesDigest": _digest(rules_json),
        "axes": {
            axis: {"unit": definition["unit"], "expected": len(definition["records"])}
            for axis, definition in axes.items()
        },
    }
    manifest = {
        "schema_version": 1,
        "source": index.relative_to(ROOT).as_posix() if index.is_relative_to(ROOT) else str(index),
        "source_digest": source_digest,
        "rules_digest": _digest(rules_json),
        "rules": RULES,
        "runtime_expectations": runtime_expectations,
        "axes": axes,
        "observed": {
            "glow_source_declarations": len(source_effects),
            "glow_color_literals": sum(len(record["color_literals"]) for record in source_effects),
            "glow_mutable_components": sum(
                component["mutable"]
                for record in source_effects
                for component in record["components"]
            )
            + sum(
                component["mutable"]
                for record in radial_effects
                for component in record["components"]
            ),
            "small_text_source_records": len(axis_sources["small-text"]),
            "tracking_source_records": len(axis_sources["tracking"]),
        },
    }
    return manifest


def _serialized(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write the generated JSON")
    mode.add_argument("--check", action="store_true", help="fail when the committed JSON is stale")
    args = parser.parse_args()
    rendered = _serialized(generate(args.index.resolve()))
    if args.write:
        args.output.write_text(rendered, encoding="utf-8")
        print(args.output)
        return 0
    if args.check:
        try:
            current = args.output.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"missing generated manifest: {args.output}", file=sys.stderr)
            return 1
        if current != rendered:
            print(
                "dashboard theme manifest is stale; run "
                "scripts/dashboard_theme_manifest.py --write",
                file=sys.stderr,
            )
            return 1
        print(args.output)
        return 0
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
