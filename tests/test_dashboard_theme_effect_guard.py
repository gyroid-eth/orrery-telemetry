from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "dashboard" / "index.html"


def _effect_guard_cases() -> dict:
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(
        r"(const THEME_AXIS_NAMES=new Set\(\[.*?\n\})\n"
        r"function initialDashboardRoute",
        html,
        re.DOTALL,
    )
    assert match, "computed effect guard must remain standalone and testable"
    harness = r"""
const entry=(before,{rendered=true,inViewport=true}={})=>
  ({before,rendered,inViewport});
const summarize=(axis,entries,after,expected=after)=>{
  const effect=summarizeThemeAxisEffect(axis,entries,after,expected);
  return {effect,error:themeAxisEffectError(effect)};
};
const lowValues=[.25,.5,.75].map(value=>summarize('small-text',[
  entry(['10px','400']),entry(['11px','500'])
],[
  [`${10+2*value}px`,value<.5?'400':'500'],
  [`${11+value}px`,'500']
]));
const noOp=summarize('glow',[
  entry(['none','rgba(0, 0, 0, 0) 0px 0px 0px','none','none'])
],[
  ['none','rgba(0, 0, 0, 0) 0px 0px 0px','none','none']
]);
const hidden=summarize('background',[
  entry(['rgb(0, 0, 0)'],{rendered:false,inViewport:false}),
  entry(['rgb(1, 1, 1)'],{rendered:true,inViewport:false})
],[['rgb(2, 2, 2)'],['rgb(3, 3, 3)']]);
const partial=summarize('tracking',[
  entry(['2px']),entry(['3px'])
],[['1px'],['3px']],[['1px'],['2px']]);
const alreadyReached=summarize('tracking',[
  entry(['1px']),entry(['3px'])
],[['1px'],['2px']],[['1px'],['2px']]);
const deferred=summarize('dim-contrast',[
  entry(['rgb(1, 1, 1)']),
  entry(['rgb(2, 2, 2)'],{rendered:true,inViewport:false}),
  entry(['rgb(3, 3, 3)'],{rendered:false,inViewport:false})
],[['rgb(4, 4, 4)'],['rgb(5, 5, 5)'],['rgb(6, 6, 6)']]);
const epsilon={
  smallNear:themeAxisComputedMatches('small-text',['10.001px','400'],['10px','400']),
  smallFar:themeAxisComputedMatches('small-text',['10.003px','400'],['10px','400']),
  weightMismatch:themeAxisComputedMatches('small-text',['10px','401'],['10px','400']),
  trackingNear:themeAxisComputedMatches('tracking',['1.001px'],['1px']),
  trackingFar:themeAxisComputedMatches('tracking',['1.003px'],['1px']),
  represented:themeAxisCanonicalChanged(['1px'],['1.001px']),
  noOp:themeAxisCanonicalChanged(['1px'],['1px'])
};
process.stdout.write(JSON.stringify({
  lowValues,noOp,hidden,partial,alreadyReached,deferred,epsilon
}));
"""
    result = subprocess.run(
        ["node", "-e", match.group(1) + "\n" + harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def test_computed_effect_guard_requires_every_preapply_visible_target_to_change():
    cases = _effect_guard_cases()
    for result in cases["lowValues"]:
        assert result == {
            "effect": {
                "unit": "computed-target",
                "evaluated": 2,
                "changed": 2,
                "rendered": 2,
                "inViewport": 2,
                "visibleExpected": 2,
                "visibleReached": 2,
                "visibleChanged": 2,
                "deferred": 0,
            },
            "error": None,
        }
    assert cases["noOp"]["error"] == "no-effective-change"
    assert cases["noOp"]["effect"]["unit"] == "effect-component"
    assert cases["hidden"]["error"] == "no-visible-targets"
    assert cases["partial"]["error"] == "effect-count-mismatch"
    assert cases["alreadyReached"] == {
        "effect": {
            "unit": "computed-target",
            "evaluated": 2,
            "changed": 1,
            "rendered": 2,
            "inViewport": 2,
            "visibleExpected": 2,
            "visibleReached": 2,
            "visibleChanged": 1,
            "deferred": 0,
        },
        "error": None,
    }


def test_computed_effect_guard_reports_deferred_targets_separately():
    result = _effect_guard_cases()["deferred"]
    assert result == {
        "effect": {
            "unit": "computed-target",
            "evaluated": 3,
            "changed": 3,
            "rendered": 2,
            "inViewport": 1,
            "visibleExpected": 1,
            "visibleReached": 1,
            "visibleChanged": 1,
            "deferred": 2,
        },
        "error": None,
    }


def test_computed_effect_guard_uses_rendering_precision_epsilons():
    assert _effect_guard_cases()["epsilon"] == {
        "smallNear": True,
        "smallFar": False,
        "weightMismatch": False,
        "trackingNear": True,
        "trackingFar": False,
        "represented": True,
        "noOp": False,
    }


def test_theme_axis_apply_fixes_membership_before_requested_effect():
    html = INDEX.read_text(encoding="utf-8")
    apply_match = re.search(
        r"function applyThemeAxis\(axis,value\)\{.*?\n\}", html, re.DOTALL
    )
    assert apply_match
    apply_source = apply_match.group(0)
    membership = apply_source.index("captureThemeAxisGlowEffectEntries()")
    expected = apply_source.index("themeAxisExpectedEffectValues(")
    requested = apply_source.index("const mutationCounts=renderThemeAxis(axis,value)")
    effect = apply_source.index("requestedEffect=summarizeThemeAxisEffect(")
    assert membership < expected < requested < effect
    assert "captureThemeAxisTokenEffectEntries(axis)" in apply_source
    assert "themeAxisEndpointCandidates" not in html
    assert "renderThemeAxis(axis,1)" not in apply_source
    assert "const effectError=themeAxisEffectError(requestedEffect)" in apply_source
    assert "captureThemeAxisGlowEffectEntries()" in apply_source
    assert "effect:requestedEffect" in apply_source
    assert "const applied=value<=0?" not in html
    assert "const affected=value<=0?" not in html
    assert "effect:result.effect||activeThemeAxisEffect" in html
    assert "mutation:activeThemeAxisMutation,effect:activeThemeAxisEffect" in html


def test_token_membership_uses_generated_consumer_eligibility_only():
    html = INDEX.read_text(encoding="utf-8")
    capture_match = re.search(
        r"function captureThemeAxisTokenEffectEntries\(axis\)\{.*?\n\}",
        html,
        re.DOTALL,
    )
    assert capture_match
    capture_source = capture_match.group(0)
    assert "THEME_AXIS_SOURCE_INVENTORY.axes[axis]" in capture_source
    assert "definition&&definition.consumers" in capture_source
    assert "if(record&&record.consumer)" in capture_source
    assert "if(!token)return" not in capture_source
    assert "themeAxisComputedMatches" not in capture_source
    assert "renderThemeAxis" not in capture_source
