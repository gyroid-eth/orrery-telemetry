"""The header pill must not show a partial reading as a complete one.

A fallback answer carries fewer windows and is as old as the source it came
from. The popover says so; the closed header is what most people look at, so
it has to say so too — this is the regression that shipped once already.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "dashboard" / "index.html"

HARNESS = r"""
const elements={'usage-pill-list':{innerHTML:''},'usage-pill':{hidden:true,title:''}};
const document={getElementById:id=>elements[id]||null};
const esc=s=>String(s);
const assetURL=name=>`assets/${name}.svg`;
const usageHidden=new Set();
console.log(JSON.stringify(run()));
"""


def _render(providers: list[dict]) -> dict:
    html = INDEX.read_text(encoding="utf-8")

    def block(pattern: str) -> str:
        match = re.search(pattern, html, re.DOTALL)
        assert match, f"missing block: {pattern}"
        return match.group(0)

    parts = [
        block(r"const USAGE_WARN=.*?\n"),
        block(r"const USAGE_LOGO=.*?\n"),
        block(r"const USAGE_NAME=.*?\n"),
        block(r"const USAGE_REASON=\{.*?\n\};\n"),
        block(r"function usageReason\(code\)\{.*?\n\}\n"),
        block(r"function usageTier\(p\).*?\n"),
        block(r"function usageLogo\(provider,cls\)\{.*?\n\}\n"),
        block(r"function usageName\(provider\)\{.*?\n\}\n"),
        block(r"const USAGE_HM=.*?\n"),
        block(r"function usageClock\(ts\)\{.*?\n\}\n"),
        block(r"function usageGroups\(provider,buckets\)\{.*?\n\}\n"),
        block(r"function usageBinding\(provider,buckets\)\{.*?\n\}\n"),
        block(r"function renderUsagePill\(providers\)\{.*?\n\}\n"),
    ]
    script = "\n".join([
        f"function run(){{ renderUsagePill({json.dumps(providers)});",
        "  return {html: elements['usage-pill-list'].innerHTML,",
        "          title: elements['usage-pill'].title,",
        "          hidden: elements['usage-pill'].hidden}; }",
        *parts,
        HARNESS,
    ])
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _render_popover(providers: list[dict]) -> dict:
    html = INDEX.read_text(encoding="utf-8")

    def block(pattern: str) -> str:
        match = re.search(pattern, html, re.DOTALL)
        assert match, f"missing block: {pattern}"
        return match.group(0)

    parts = [
        block(r"const USAGE_WARN=.*?\n"),
        block(r"const USAGE_LOGO=.*?\n"),
        block(r"const USAGE_NAME=.*?\n"),
        block(r"const USAGE_REASON=\{.*?\n\};\n"),
        block(r"function usageReason\(code\)\{.*?\n\}\n"),
        block(r"function usageTier\(p\).*?\n"),
        block(r"function usageLogo\(provider,cls\)\{.*?\n\}\n"),
        block(r"function usageName\(provider\)\{.*?\n\}\n"),
        block(r"const USAGE_HM=.*?\n"),
        block(r"const USAGE_SOURCE=\{.*?\n\};\n"),
        block(r"function usageSource\(source\)\{.*?\n\}\n"),
        block(r"function usageClock\(ts\)\{.*?\n\}\n"),
        block(r"function usageReset\(ts,compact\)\{.*?\n\}\n"),
        block(r"function usageGroups\(provider,buckets\)\{.*?\n\}\n"),
        block(r"function usageBinding\(provider,buckets\)\{.*?\n\}\n"),
        block(r"function usageDial\(b,stale\)\{.*?\n\}\n"),
        block(r"function renderUsagePopover\(data\)\{.*?\n\}\n"),
    ]
    harness = r"""
const elements={'usage-body':{innerHTML:''},'usage-kick':{textContent:''}};
const document={getElementById:id=>elements[id]||null};
const esc=s=>String(s);
const assetURL=name=>`assets/${name}.svg`;
console.log(JSON.stringify(run()));
"""
    script = "\n".join([
        f"function run(){{ renderUsagePopover({json.dumps({'providers': providers})});",
        "  return {html: elements['usage-body'].innerHTML,",
        "          kick: elements['usage-kick'].textContent}; }",
        *parts,
        harness,
    ])
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _provider(status: str, reason: str = "", observed_at: int = 1_789_380_000) -> dict:
    return {
        "provider": "claude",
        "status": status,
        "reason": reason,
        "observed_at": observed_at,
        "buckets": [{"id": "five_hour", "label": "5h", "remaining_percent": 57.0,
                     "resets_at": observed_at + 3600, "window_seconds": 18000}],
    }


def test_a_complete_reading_carries_no_partial_marks():
    rendered = _render([_provider("ok")])

    assert "partial" not in rendered["html"]
    assert "up-flag" not in rendered["html"]
    assert "partial" not in rendered["title"]


def test_a_partial_reading_is_marked_dated_and_explained():
    rendered = _render([_provider("degraded", "fallback_rate_limited")])

    assert "up-p partial" in rendered["html"] or "partial" in rendered["html"]
    assert "up-flag" in rendered["html"], "the closed header needs a visible mark"
    assert "partial" in rendered["title"]
    assert "observed" in rendered["title"], "a fallback number is as old as its source"
    assert "rate limited" in rendered["title"], "and it must say why the full source failed"


def test_an_unmapped_reason_is_never_shown_raw():
    rendered = _render([_provider("degraded", "fallback_something_new")])

    assert "something_new" not in rendered["title"]
    assert "something new" in rendered["title"]


def test_an_unknown_window_keeps_its_name_and_time_without_a_number_or_ring():
    provider = _provider("stale", "fallback_rate_limited")
    provider["degraded"] = True
    provider["partial"] = False
    provider["buckets"] = [{
        "id": "model-fable",
        "label": "Fable",
        "resets_at": provider["observed_at"] + 3600,
        "observed_at": provider["observed_at"],
        "source": "claude-account-usage",
        "value_status": "unknown",
    }]

    rendered = _render_popover([provider])

    assert "Fable" in rendered["html"]
    assert "current value unknown" in rendered["html"]
    assert "last observed" in rendered["html"]
    assert "account" in rendered["html"]
    assert "<circle" not in rendered["html"]
    assert "% left" not in rendered["html"]


def test_retained_full_window_list_is_degraded_without_claiming_windows_are_missing():
    provider = _provider("ok", "fallback_rate_limited")
    provider["degraded"] = True
    provider["partial"] = False
    provider["buckets"].append({
        "id": "model-fable",
        "label": "Fable",
        "remaining_percent": 23,
        "resets_at": provider["observed_at"] + 3600,
        "observed_at": provider["observed_at"] - 600,
        "source": "claude-account-usage",
        "value_status": "previous",
    })

    rendered = _render_popover([provider])

    assert "DEGRADED" in rendered["html"]
    assert "PARTIAL" not in rendered["html"]
    assert "some windows are missing" not in rendered["html"]
    assert "previous value" in rendered["html"]
    assert "account" in rendered["html"]


def test_stale_and_degraded_are_rendered_as_two_independent_axes():
    provider = _provider("stale", "fallback_rate_limited")
    provider["degraded"] = True
    provider["partial"] = False

    rendered = _render_popover([provider])

    assert "stale" in rendered["html"]
    assert "degraded" in rendered["html"]


def test_all_unknown_windows_keep_the_closed_pill_visible_without_a_percentage():
    provider = _provider("stale", "fallback_rate_limited")
    provider["degraded"] = True
    provider["partial"] = False
    provider["buckets"] = [{
        "id": "model-fable",
        "label": "Fable",
        "observed_at": provider["observed_at"],
        "source": "claude-account-usage",
        "value_status": "unknown",
    }]

    rendered = _render([provider])

    assert rendered["hidden"] is False
    assert "—" in rendered["html"]
    assert "%" not in rendered["html"]
    assert "current value unknown" in rendered["title"]


def test_a_previous_binding_is_visibly_marked_in_the_closed_pill():
    provider = _provider("ok")
    provider["buckets"][0]["value_status"] = "previous"
    provider["buckets"][0]["observed_at"] = provider["observed_at"] - 600

    rendered = _render([provider])

    assert "PREV" in rendered["html"]
    assert "stale" in rendered["title"]
