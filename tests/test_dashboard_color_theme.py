from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
THEME_CORE = ROOT / "dashboard" / "theme_core.js"
THEME_CONTROLLER = ROOT / "dashboard" / "theme_controller.js"
THEME_CSS = ROOT / "dashboard" / "theme_light.css"


def _node(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def test_warm_paper_tokens_and_contrast_are_repo_contracts():
    result = _node(
        """
const core=require('./dashboard/theme_core.js');
const theme=core.deriveWarmPaperLightTheme();
process.stdout.write(JSON.stringify({vars:theme.cssVariables,report:core.contrastReport(theme)}));
"""
    )
    assert result["vars"] == {
        "--bg": "#f5efe1",
        "--panel": "#e8dfcd",
        "--panel-2": "#ddd2bc",
        "--elev": "#d1c3a9",
        "--ink": "#010100",
        "--ink-dim": "#3a3528",
        "--ink-faint": "#545148",
        "--amber": "#704a00",
        "--amber-deep": "#704a00",
        "--amber-glow": "transparent",
        "--ln-local": "#005c50",
        "--ln-remote": "#673f8d",
        "--ln-delegate": "#335b16",
        "--alert": "#a6060e",
        "--cyan": "#005a65",
        "--void": "#f5efe1",
        "--bone": "#010100",
        "--bone-dim": "#3a3528",
        "--line": "rgb(1 1 0 / 0.16)",
        "--line-soft": "rgb(1 1 0 / 0.08)",
        "--hair": "rgb(1 1 0 / 0.16)",
        "--hair-2": "rgb(1 1 0 / 0.08)",
        "--glow-a": "none",
        "--glow-c": "none",
        "--theme-border-control": "#5f5c52",
        "--theme-surface-terminal": "#fdf7ea",
        "--theme-shadow-low": "rgb(1 1 0 / 0.12)",
        "--theme-shadow-high": "rgb(1 1 0 / 0.08)",
        "--theme-ink-rgb": "1 1 0",
        "--theme-accent-rgb": "112 74 0",
    }
    for role, measurement in result["report"].items():
        assert measurement["ratio"] >= measurement["target"], role
    assert min(
        measurement["ratio"]
        for role, measurement in result["report"].items()
        if role != "control"
    ) >= 4.5
    assert result["report"]["control"]["ratio"] >= 3


def test_light_stylesheet_is_additive_and_role_driven():
    source = THEME_CSS.read_text(encoding="utf-8")
    without_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    selectors = re.findall(r"(?:^|\})([^{}]+)\{", without_comments)
    assert selectors
    for selector_group in selectors:
        assert all(
            selector.strip().startswith('html[data-color-theme="light"]')
            for selector in selector_group.split(",")
        ), selector_group
    for value in re.findall(r"(?<!-)color\s*:\s*([^;}]+)", without_comments):
        assert "var(" in value, value
    assert "color-scheme:light" in source
    assert "--theme-border-control" in source
    assert "--theme-surface-terminal" in source
    assert ".node .ctx-arc-glow{display:none}" in source
    assert '#demo-card .box' in source
    assert '#demo-strip' in source


def test_embedded_receiver_validates_source_origin_and_reverses_cleanly():
    result = _node(
        """
const core=require('./dashboard/theme_core.js');
const listeners={};const sent=[];const values=new Map();
const parentWindow={postMessage:(data,origin)=>sent.push({data,origin})};
const root={dataset:{},style:{
  setProperty:(key,value)=>values.set(key,value),removeProperty:key=>values.delete(key)
},removeAttribute(name){
  if(name==='data-color-theme')delete this.dataset.colorTheme;
  if(name==='data-color-theme-preference')delete this.dataset.colorThemePreference;
}};
global.location={search:'?embed=1',origin:'http://dashboard.test'};
global.document={documentElement:root,readyState:'complete'};
global.window={parent:parentWindow,AgentStackColorThemeCore:core,
  addEventListener:(name,listener)=>{listeners[name]=listener;}};
require('./dashboard/theme_controller.js');
const api=window.AgentStackColorTheme;
const normalized={
  light:api.normalize({type:'orrery-color-theme',version:1,preference:'light',resolved:'light'}),
  system:api.normalize({type:'orrery-color-theme',version:1,preference:'system',resolved:'dark'}),
  mismatch:api.normalize({type:'orrery-color-theme',version:1,preference:'dark',resolved:'light'}),
};
listeners.message({source:{},origin:location.origin,data:{
  type:'orrery-color-theme',version:1,preference:'light',resolved:'light'
}});
const afterWrong={count:values.size,theme:root.dataset.colorTheme};
listeners.message({source:parentWindow,origin:'http://other.test',data:{
  type:'orrery-color-theme',version:1,preference:'light',resolved:'light'
}});
const afterOrigin={count:values.size,theme:root.dataset.colorTheme};
listeners.message({source:parentWindow,origin:location.origin,data:{
  type:'orrery-color-theme',version:1,preference:'light',resolved:'light'
}});
const afterLight={count:values.size,theme:root.dataset.colorTheme,
  preference:root.dataset.colorThemePreference,bg:values.get('--bg')};
listeners.message({source:parentWindow,origin:location.origin,data:{
  type:'orrery-color-theme',version:1,preference:'dark',resolved:'dark'
}});
const afterDark={count:values.size,theme:root.dataset.colorTheme,
  preference:root.dataset.colorThemePreference};
process.stdout.write(JSON.stringify({normalized,afterWrong,afterOrigin,afterLight,afterDark,sent}));
"""
    )
    assert result["normalized"] == {
        "light": {"preference": "light", "resolved": "light"},
        "system": {"preference": "system", "resolved": "dark"},
        "mismatch": None,
    }
    assert result["afterWrong"] == {"count": 0}
    assert result["afterOrigin"] == {"count": 0}
    assert result["afterLight"] == {
        "count": 30,
        "theme": "light",
        "preference": "light",
        "bg": "#f5efe1",
    }
    assert result["afterDark"] == {"count": 0, "preference": "dark"}
    assert result["sent"][0]["data"] == {
        "type": "orrery-color-theme-ready",
        "version": 1,
    }
    assert [message["data"]["ok"] for message in result["sent"][1:]] == [True, True]


def test_dashboard_server_exposes_only_named_theme_assets():
    source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert '"/theme_core.js"' in source
    assert '"/theme_controller.js"' in source
    assert '"/theme_light.css"' in source
    assert "elif path in THEME_ASSETS:" in source


def test_static_demo_bundle_carries_theme_assets():
    source = (ROOT / "dashboard" / "demo" / "build.sh").read_text(encoding="utf-8")
    assert '"$DASH/theme_core.js"' in source
    assert '"$DASH/theme_controller.js"' in source
    assert '"$DASH/theme_light.css"' in source


def test_color_theme_assets_have_valid_javascript_syntax():
    for path in (THEME_CORE, THEME_CONTROLLER):
        subprocess.run(
            ["node", "--check", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
