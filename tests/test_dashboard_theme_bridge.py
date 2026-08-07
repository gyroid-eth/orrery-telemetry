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


def _profile_values(*, small: float | None = None, tracking: float | None = None) -> dict:
    return {
        "dim-contrast": None,
        "small-text": small,
        "tracking": tracking,
        "glow": None,
        "background": None,
    }


def _theme_bridge_cases() -> dict:
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(
        r"(const THEME_AXIS_IDS=Object\.freeze\(\[.*?\n\})"
        r"\nfunction initialDashboardRoute",
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
  tiny:normalizeThemeAxisMessage({type:'agentstack-theme-axis',version:1,axis,value:Number.MIN_VALUE}),
  fraction:normalizeThemeAxisMessage({type:'agentstack-theme-axis',version:1,axis,value:0.3333}),
  one:normalizeThemeAxisMessage({type:'agentstack-theme-axis',version:1,axis,value:1}),
  reset:normalizeThemeAxisMessage({type:'agentstack-theme-axis',version:1,axis,value:null})
}));
const rejected=[
  null,
  {type:'other',version:1,axis:'glow',value:1},
  {type:'agentstack-theme-axis',version:2,axis:'glow',value:1},
  {type:'agentstack-theme-axis',version:1,axis:'unknown',value:1},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:-0.001},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:1.001},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:'1'},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:NaN},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:Infinity}
].map(normalizeThemeAxisMessage);
const errors=[
  {type:'other',version:1,axis:'glow',value:1},
  {type:'agentstack-theme-axis',version:2,axis:'glow',value:1},
  {type:'agentstack-theme-axis',version:1,axis:'unknown',value:1},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:-0.001},
  {type:'agentstack-theme-axis',version:1,axis:'glow',value:1.001},
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
const profileValues=(small=null,tracking=null)=>({
  'dim-contrast':null,'small-text':small,tracking,glow:null,background:null
});
const profiles={
  accepted:[
    {type:'agentstack-theme-profile',version:1,requestId:'reset',values:profileValues()},
    {type:'agentstack-theme-profile',version:1,requestId:'small',values:profileValues(.25)},
    {type:'agentstack-theme-profile',version:1,requestId:'tracking',values:profileValues(null,.5)},
    {type:'agentstack-theme-profile',version:1,requestId:'both',values:profileValues(.75,1)},
    {type:'agentstack-theme-profile',version:1,requestId:'legacy',values:{
      ...profileValues(),'dim-contrast':.5}}
  ].map(normalizeThemeProfileMessage),
  errors:[
    {type:'agentstack-theme-profile',version:1,requestId:'',values:profileValues()},
    {type:'agentstack-theme-profile',version:1,requestId:'missing',values:{tracking:.5}},
    {type:'agentstack-theme-profile',version:1,requestId:'extra',values:{
      ...profileValues(),extra:null}},
    {type:'agentstack-theme-profile',version:1,requestId:'mixed',values:{
      ...profileValues(.5),'dim-contrast':.5}},
    {type:'agentstack-theme-profile',version:1,requestId:'range',values:profileValues(1.001)}
  ].map(themeProfileMessageError)
};
process.stdout.write(JSON.stringify({axes,accepted,rejected,errors,guards,mix,shadow,profiles}));
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
        r"(const THEME_AXIS_IDS=Object\.freeze\(\[.*?\n\})"
        r"\nfunction initialDashboardRoute",
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
            "tiny": {"axis": axis, "value": 5e-324},
            "fraction": {"axis": axis, "value": 0.3333},
            "one": {"axis": axis, "value": 1},
            "reset": {"axis": axis, "value": None},
        }
    assert cases["rejected"] == [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    ]
    assert cases["errors"] == [
        "invalid-message",
        "unsupported-version",
        "unknown-axis",
        "invalid-value",
        "invalid-value",
        "invalid-value",
    ]
    assert cases["guards"] == {
        "allowed": True,
        "topLevel": False,
        "wrongSource": False,
        "wrongOrigin": False,
    }
    assert cases["profiles"]["accepted"] == [
        {"requestId": "reset", "values": _profile_values()},
        {"requestId": "small", "values": _profile_values(small=0.25)},
        {"requestId": "tracking", "values": _profile_values(tracking=0.5)},
        {
            "requestId": "both",
            "values": _profile_values(small=0.75, tracking=1),
        },
        {
            "requestId": "legacy",
            "values": {**_profile_values(), "dim-contrast": 0.5},
        },
    ]
    assert cases["profiles"]["errors"] == [
        "invalid-request-id",
        "invalid-values",
        "invalid-values",
        "invalid-profile-combination",
        "invalid-value",
    ]


def test_theme_axis_out_of_range_reject_preserves_last_valid_state():
    html = INDEX.read_text(encoding="utf-8")
    apply_message = re.search(
        r"(async function applyThemeAxisMessage\(data\)\{.*?\n\})",
        html,
        re.DOTALL,
    )
    assert apply_message, "theme message application must remain testable"
    harness = r"""
let state={axis:null,value:null};
const applied=[];
async function themeAxisInventoryDigestError(){return null;}
function applyThemeAxis(axis,value){
  state={axis,value};
  applied.push({...state});
  return {ok:true};
}
(async()=>{
  const valid=await applyThemeAxisMessage({
    type:'agentstack-theme-axis',version:1,axis:'background',value:0.4
  });
  const below=await applyThemeAxisMessage({
    type:'agentstack-theme-axis',version:1,axis:'background',value:-0.001
  });
  const above=await applyThemeAxisMessage({
    type:'agentstack-theme-axis',version:1,axis:'background',value:1.001
  });
  process.stdout.write(JSON.stringify({valid,below,above,state,applied}));
})().catch(error=>{console.error(error);process.exit(1);});
"""
    result = subprocess.run(
        [
            "node",
            "-e",
            _theme_bridge_source() + "\n" + apply_message.group(1) + "\n" + harness,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "valid": {"ok": True},
        "below": {"ok": False, "reason": "invalid-value"},
        "above": {"ok": False, "reason": "invalid-value"},
        "state": {"axis": "background", "value": 0.4},
        "applied": [{"axis": "background", "value": 0.4}],
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
    legacy_apply = re.search(
        r"function applyThemeAxis\(axis,value\)\{.*?\n\}", html, re.DOTALL
    )
    assert legacy_apply
    assert "const values=emptyThemeProfileValues()" in legacy_apply.group(0)
    assert "if(value!==null)values[axis]=value" in legacy_apply.group(0)
    assert "const profile=applyThemeProfile(values)" in legacy_apply.group(0)
    assert "if(themeAxisStyleNode){themeAxisStyleNode.remove();themeAxisStyleNode=null;}" in html
    assert re.search(
        r"restoreThemeAxisOverrides\(\);setActiveThemeProfile\(emptyThemeProfileValues\(\)\);",
        html,
    )
    assert "event.source===parentWindow" in html
    assert "event.origin===expectedOrigin" in html
    assert "observeThemeProfileAdditions(activeThemeProfileValues)" in html
    assert "themeAxisObserver.observe(document.body,{childList:true,subtree:true});" in html


def _runtime_inventory() -> dict:
    html = INDEX.read_text(encoding="utf-8")
    inventory = re.search(
        r'<script id="agentstack-theme-axis-inventory" '
        r'type="application/json">\n(.*?)\n</script>',
        html,
        re.DOTALL,
    )
    assert inventory, "generated runtime inventory must be embedded"
    return json.loads(inventory.group(1))


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
    runtime = _runtime_inventory()
    assert runtime == manifest["runtime_inventory"]
    rules_canonical = json.dumps(
        runtime["rules"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert hashlib.sha256(rules_canonical.encode()).hexdigest() == (
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
    assert "runtime_expectations" not in manifest
    assert '"expected"' not in manifest_path.read_text(encoding="utf-8")
    assert "THEME_AXIS_SOURCE_EXPECTATIONS" not in html
    assert "expected:80" not in html
    assert "expected:9" not in html
    assert "expected:2" not in html
    assert all(axes[axis]["records"] for axis in THEME_AXIS_NAMES)
    expected_units = {
        "dim-contrast": "token-write",
        "small-text": "declaration",
        "tracking": "declaration",
        "glow": "declaration",
        "background": "token-write",
    }
    assert {axis: axes[axis]["unit"] for axis in THEME_AXIS_NAMES} == expected_units
    assert {
        axis: runtime["axes"][axis]["unit"] for axis in THEME_AXIS_NAMES
    } == expected_units
    for axis in THEME_AXIS_NAMES:
        assert len(runtime["axes"][axis]["records"]) == len(axes[axis]["records"])
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


def test_theme_axis_inventory_check_detects_and_recovers_from_stale_css(tmp_path):
    script = ROOT / "scripts" / "dashboard_theme_manifest.py"
    index = tmp_path / "index.html"
    manifest = tmp_path / "theme_effect_manifest.json"
    original = INDEX.read_text(encoding="utf-8")
    index.write_text(original, encoding="utf-8")
    subprocess.run(
        ["python3", str(script), "--index", str(index), "--output", str(manifest), "--write"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    baseline = index.read_text(encoding="utf-8")
    stale = baseline.replace(
        "0 0 8px rgba(255,90,77,.4)",
        "0 0 9px rgba(255,90,77,.4)",
        1,
    )
    assert stale != baseline
    index.write_text(stale, encoding="utf-8")
    failed = subprocess.run(
        ["python3", str(script), "--index", str(index), "--output", str(manifest), "--check"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert failed.returncode == 1
    assert "theme_effect_manifest.json is stale" in failed.stderr
    assert "embedded theme inventory is stale" in failed.stderr
    assert '"blur": 8.0' in failed.stderr
    assert '"blur": 9.0' in failed.stderr
    index.write_text(baseline, encoding="utf-8")
    subprocess.run(
        ["python3", str(script), "--index", str(index), "--output", str(manifest), "--check"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
