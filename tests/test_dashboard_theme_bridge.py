from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "dashboard" / "index.html"
THEME_AXIS_NAMES = (
    "dim-contrast",
    "small-text",
    "tracking",
    "glow",
    "background",
)


def _theme_bridge_cases() -> dict:
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(
        r"(const THEME_AXIS_NAMES=new Set\(\[.*?\n\})\nfunction initialDashboardRoute",
        html,
        re.DOTALL,
    )
    assert match, "theme message validation must remain standalone and testable"
    harness = r"""
const parentWindow={name:'parent'};
const allowedEvent={source:parentWindow,origin:'http://dashboard.test'};
const axes=[...THEME_AXIS_NAMES];
const accepted=axes.map(axis=>({
  zero:normalizeThemeAxisMessage({type:'agentstack-theme-axis',version:1,axis,value:0}),
  one:normalizeThemeAxisMessage({type:'agentstack-theme-axis',version:1,axis,value:1}),
  reset:normalizeThemeAxisMessage({type:'agentstack-theme-axis',version:1,axis,value:null})
}));
const rejected=[
  null,
  {type:'other',version:1,axis:'glow',value:1},
  {type:'agentstack-theme-axis',version:2,axis:'glow',value:1},
  {type:'agentstack-theme-axis',version:1,axis:'unknown',value:1},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:-0.01},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:1.01},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:'1'},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:NaN},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:Infinity}
].map(normalizeThemeAxisMessage);
const errors=[
  {type:'other',version:1,axis:'glow',value:1},
  {type:'agentstack-theme-axis',version:2,axis:'glow',value:1},
  {type:'agentstack-theme-axis',version:1,axis:'unknown',value:1},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:'1'}
].map(themeAxisMessageError);
const guards={
  allowed:themeAxisMessageAllowed(allowedEvent,true,parentWindow,'http://dashboard.test'),
  topLevel:themeAxisMessageAllowed(allowedEvent,false,parentWindow,'http://dashboard.test'),
  wrongSource:themeAxisMessageAllowed({...allowedEvent,source:{}},true,parentWindow,'http://dashboard.test'),
  wrongOrigin:themeAxisMessageAllowed({...allowedEvent,origin:'http://other.test'},true,parentWindow,'http://dashboard.test')
};
const mix=themeAxisColorMix('#000','#fff',0.3333);
const shadow=attenuateThemeShadow(
  'rgba(10, 100, 200, 0.5) 0px 0px 5px, '
  +'rgb(1, 2, 3) 0px 0px 2px, rgba(200, 20, 20, 0.5) 0px 2px 4px',0.4
);
process.stdout.write(JSON.stringify({axes,accepted,rejected,errors,guards,mix,shadow}));
"""
    result = subprocess.run(
        ["node", "-e", match.group(1) + "\n" + harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def _theme_bridge_source() -> str:
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(
        r"(const THEME_AXIS_NAMES=new Set\(\[.*?\n\})\nfunction initialDashboardRoute",
        html,
        re.DOTALL,
    )
    assert match, "theme derivations must remain standalone and testable"
    return match.group(1)


def test_theme_axis_bridge_validates_schema_source_and_origin():
    cases = _theme_bridge_cases()
    assert cases["axes"] == [
        "dim-contrast",
        "small-text",
        "tracking",
        "glow",
        "background",
    ]
    for axis, values in zip(cases["axes"], cases["accepted"], strict=True):
        assert values == {
            "zero": {"axis": axis, "value": 0},
            "one": {"axis": axis, "value": 1},
            "reset": {"axis": axis, "value": None},
        }
    assert cases["rejected"] == [
        None,
        None,
        None,
        None,
        {"axis": "glow", "value": 0},
        {"axis": "glow", "value": 1},
        None,
        None,
        None,
    ]
    assert cases["errors"] == [
        "invalid-message",
        "unsupported-version",
        "unknown-axis",
        "invalid-value",
    ]
    assert cases["guards"] == {
        "allowed": True,
        "topLevel": False,
        "wrongSource": False,
        "wrongOrigin": False,
    }


def test_theme_axis_derivations_are_deterministic():
    cases = _theme_bridge_cases()
    assert cases["mix"] == "rgba(54.105, 54.105, 54.105, 1)"
    assert cases["shadow"] == (
        "rgba(10, 100, 200, 0.34) 0px 0px 3.5px, "
        "rgb(1, 2, 3) 0px 0px 2px, rgba(200, 20, 20, 0.5) 0px 2px 4px"
    )


def test_runtime_effect_classifier_matches_generated_manifest():
    manifest = json.loads(
        (ROOT / "dashboard" / "theme_effect_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    components = [
        {
            "selector": record["selector"],
            "component": component["value"],
            "category": component["category"],
            "mutable": component["mutable"],
        }
        for record in manifest["axes"]["glow"]["records"]
        for component in record["components"]
    ]
    radial_components = [
        component["value"]
        for record in manifest["axes"]["glow"]["radial_records"]
        for component in record["components"]
    ]
    harness = f"""
const components={json.dumps(components)};
const radialComponents={json.dumps(radial_components)};
const mismatches=components.filter(row=>{{
  const actual=themeAxisEffectComponent(row.selector,row.component);
  return actual.category!==row.category||actual.mutable!==row.mutable;
}});
const radialMismatch=radialComponents.filter(component=>{{
  const actual=transformThemeRadialGradient(component,1);
  return actual.expected!==1||actual.applied!==1;
}});
process.stdout.write(JSON.stringify({{mismatches,radialMismatch}}));
"""
    result = subprocess.run(
        ["node", "-e", _theme_bridge_source() + "\n" + harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == {"mismatches": [], "radialMismatch": []}


def test_theme_axis_default_is_unapplied_and_reset_is_reversible():
    html = INDEX.read_text(encoding="utf-8")
    assert "<html data-agentstack-theme-axis" not in html
    assert "<body data-agentstack-theme-axis" not in html
    assert "if(value===null){\n    if(activeThemeAxis===axis){" in html
    assert "if(themeAxisStyleNode){themeAxisStyleNode.remove();themeAxisStyleNode=null;}" in html
    assert re.search(
        r"restoreThemeAxisOverrides\(\);\s+setActiveThemeAxis\(null,null\);",
        html,
    )
    assert "event.source===parentWindow" in html
    assert "event.origin===expectedOrigin" in html
    assert "themeAxisObserver.observe(document.body,{childList:true,subtree:true});" in html


def _runtime_inventory_constants() -> dict:
    html = INDEX.read_text(encoding="utf-8")
    expectations = re.search(
        r"(const THEME_AXIS_SOURCE_EXPECTATIONS=\{.*?\n\};)",
        html,
        re.DOTALL,
    )
    rules = re.search(
        r"(const THEME_AXIS_RULES_CANONICAL=String\.raw`.*?`;)",
        html,
        re.DOTALL,
    )
    assert expectations and rules, "runtime inventory constants must be embedded"
    result = subprocess.run(
        [
            "node",
            "-e",
            expectations.group(1)
            + "\n"
            + rules.group(1)
            + "\nprocess.stdout.write(JSON.stringify({"
            + "expectations:THEME_AXIS_SOURCE_EXPECTATIONS,"
            + "rulesCanonical:THEME_AXIS_RULES_CANONICAL}));",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def test_theme_axis_inventory_is_regenerated_and_nonempty():
    script = ROOT / "scripts" / "dashboard_theme_manifest.py"
    manifest_path = ROOT / "dashboard" / "theme_effect_manifest.json"
    subprocess.run(
        ["python3", str(script), "--check"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime = _runtime_inventory_constants()
    assert runtime["expectations"] == manifest["runtime_expectations"]
    assert json.loads(runtime["rulesCanonical"]) == manifest["rules"]
    assert hashlib.sha256(runtime["rulesCanonical"].encode()).hexdigest() == (
        manifest["rules_digest"]
    )
    html = INDEX.read_text(encoding="utf-8")
    css = re.search(
        r'<style id="agentstack-dashboard-styles">(.*?)</style>',
        html,
        re.DOTALL,
    )
    assert css
    assert hashlib.sha256(css.group(1).encode()).hexdigest() == (
        manifest["source_digest"]
    )
    axes = manifest["axes"]
    assert all(axes[axis]["records"] for axis in THEME_AXIS_NAMES)
    glow_records = axes["glow"]["records"]
    assert manifest["observed"]["glow_source_declarations"] == len(glow_records)
    assert manifest["observed"]["glow_color_literals"] == sum(
        len(record["color_literals"]) for record in glow_records
    )
    assert manifest["observed"]["glow_mutable_components"] == (
        sum(
            component["mutable"]
            for record in glow_records
            for component in record["components"]
        )
        + sum(
            component["mutable"]
            for record in axes["glow"]["radial_records"]
            for component in record["components"]
        )
    )
