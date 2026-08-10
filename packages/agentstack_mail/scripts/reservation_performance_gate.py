"""Measure the bounded reservation probe against a real Git workspace.

The release gate is the deterministic 57-path tracked-file sample. It passes
only when every probe is complete and matched and total wall time is at most
three seconds. The live-pattern snapshot is diagnostic and is reported without
changing the release decision.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

app = importlib.import_module("agentstack_mail.app")


LIVE_PATTERN_SNAPSHOT = [
    "10_Reference/Lit/=rico-guevaraHummingbirdTongueFluid2011=.md",
    "10_Reference/MDPapers/Rico-Guevara and Rubega - 2011 - The hummingbird "
    "tongue is a fluid trap, not a capillary tube.md",
    "10_Reference/MDPapers/pdf-mistral-images/*[Rr]ico-[Gg]uevara*",
    "../MDPapers-images/*[Rr]ico-[Gg]uevara*",
    "10_Reference/Lit/=galloStigmergyBehavioralFlexibility2021a=.md",
    "10_Reference/MDPapers/Gallo and Chittka - 2021 - Stigmergy versus "
    "behavioral flexibility and planning in honeybee comb construction.md",
    "10_Reference/MDPapers/pdf-mistral-images/*[Gg]allo*",
    "../MDPapers-images/*[Gg]allo*",
    "10_Reference/Lit/=aristoffWaterLappingFelines2011=.md",
    "10_Reference/MDPapers/Aristoff et al. - 2011 - On the water lapping of "
    "felines and the water running of lizards.md",
    "10_Reference/MDPapers/pdf-mistral-images/*[Aa]ristoff*",
    "../MDPapers-images/*[Aa]ristoff*",
    "10_Reference/Lit/=huangEvolutionWrinklesHard2004=.md",
    "10_Reference/MDPapers/Huang et al. - 2004 - Evolution of wrinkles in hard "
    "films on soft substrates.md",
    "10_Reference/MDPapers/pdf-mistral-images/*[Hh]uang*2004*",
    "../MDPapers-images/*[Hh]uang*2004*",
]


def tracked_sample(root: Path, count: int) -> list[str]:
    raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
    paths = [
        item.decode("utf-8", "surrogateescape")
        for item in raw.split(b"\0")
        if item
    ]
    preferred = [
        path
        for path in paths
        if path.startswith("10_Reference/")
        and path.lower().endswith((".md", ".png", ".jpg", ".jpeg", ".webp"))
    ]
    candidates = preferred if len(preferred) >= count else paths
    step = max(1, len(candidates) // count)
    sample = candidates[::step][:count]
    if len(sample) != count:
        raise RuntimeError(f"wanted {count} tracked paths, found {len(sample)}")
    return sample


def measure(root: Path, label: str, patterns: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    results = asyncio.run(
        app._probe_reservation_activities(
            root,
            app._find_repo_root_if_available(root),
            patterns,
            recent_after=datetime.now(timezone.utc) - timedelta(minutes=15),
        )
    )
    wall_seconds = time.perf_counter() - started
    digest_payload = [
        {
            "pattern": pattern,
            "matched": result.matched,
            "filesystem": (
                result.fs_activity.isoformat() if result.fs_activity else None
            ),
            "git": result.git_activity.isoformat() if result.git_activity else None,
            "probe_complete": result.probe_complete,
        }
        for pattern, result in zip(patterns, results, strict=True)
    ]
    return {
        "set": label,
        "count": len(patterns),
        "wall_seconds": round(wall_seconds, 4),
        "matched": sum(result.matched for result in results),
        "probe_complete": sum(result.probe_complete for result in results),
        "activity_unknown": sum(not result.probe_complete for result in results),
        "unknown_patterns": [
            pattern
            for pattern, result in zip(patterns, results, strict=True)
            if not result.probe_complete
        ],
        "unmatched_patterns": [
            pattern
            for pattern, result in zip(patterns, results, strict=True)
            if not result.matched
        ],
        "git_results": sum(result.git_activity is not None for result in results),
        "result_sha256": hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--threshold-seconds", type=float, default=3.0)
    parser.add_argument("--concrete-count", type=int, default=57)
    parser.add_argument("--live-patterns-json", type=Path)
    parser.add_argument("--skip-live-snapshot", action="store_true")
    args = parser.parse_args()

    root = args.workspace.expanduser().resolve()
    concrete = tracked_sample(root, args.concrete_count)
    concrete_result = measure(root, "57-concrete", concrete)
    gate_passed = bool(
        concrete_result["count"] == args.concrete_count
        and concrete_result["matched"] == args.concrete_count
        and concrete_result["probe_complete"] == args.concrete_count
        and concrete_result["wall_seconds"] <= args.threshold_seconds
    )
    print(json.dumps(concrete_result, ensure_ascii=False, sort_keys=True))

    if not args.skip_live_snapshot:
        patterns = LIVE_PATTERN_SNAPSHOT
        if args.live_patterns_json is not None:
            loaded = json.loads(args.live_patterns_json.read_text(encoding="utf-8"))
            if not isinstance(loaded, list) or not all(
                isinstance(item, str) for item in loaded
            ):
                raise ValueError("live patterns JSON must be an array of strings")
            patterns = loaded
        print(
            json.dumps(
                measure(root, "live-snapshot", patterns),
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    print(
        json.dumps(
            {
                "gate": "reservation-57-path-wall-time",
                "threshold_seconds": args.threshold_seconds,
                "passed": gate_passed,
            },
            sort_keys=True,
        )
    )
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
