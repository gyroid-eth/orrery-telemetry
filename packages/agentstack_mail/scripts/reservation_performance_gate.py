"""Measure the bounded reservation probe against a real Git workspace.

The release gate repeats the deterministic 57-path tracked-file sample five
times. It passes when the median wall time is at most six seconds and a
majority of runs are fully matched and complete. This catches a return to the
9.5-second serial implementation without failing on one loaded-machine
outlier. The live-pattern snapshot is diagnostic and does not affect the gate.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import statistics
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
    result_shape = [
        {
            "pattern": pattern,
            "matched": result.matched,
            "filesystem_present": result.fs_activity is not None,
            "git_present": result.git_activity is not None,
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
        "input_sha256": hashlib.sha256(
            json.dumps(patterns, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "result_shape_sha256": hashlib.sha256(
            json.dumps(result_shape, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest(),
    }


def summarize_concrete_runs(
    results: list[dict[str, Any]],
    *,
    expected_count: int,
) -> dict[str, Any]:
    wall_seconds = [float(result["wall_seconds"]) for result in results]
    complete_runs = sum(
        result["matched"] == expected_count
        and result["probe_complete"] == expected_count
        for result in results
    )
    required_complete_runs = len(results) // 2 + 1
    return {
        "set": "57-concrete-summary",
        "count": expected_count,
        "repetitions": len(results),
        "wall_seconds": wall_seconds,
        "median_wall_seconds": round(statistics.median(wall_seconds), 4),
        "max_wall_seconds": max(wall_seconds),
        "complete_runs": complete_runs,
        "required_complete_runs": required_complete_runs,
        "input_sha256": results[0]["input_sha256"],
    }


def passes_gate(summary: dict[str, Any], *, threshold_seconds: float) -> bool:
    return bool(
        summary["complete_runs"] >= summary["required_complete_runs"]
        and summary["median_wall_seconds"] <= threshold_seconds
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--threshold-seconds", type=float, default=6.0)
    parser.add_argument("--concrete-count", type=int, default=57)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--live-patterns-json", type=Path)
    parser.add_argument("--skip-live-snapshot", action="store_true")
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be at least 1")

    root = args.workspace.expanduser().resolve()
    concrete = tracked_sample(root, args.concrete_count)
    concrete_results: list[dict[str, Any]] = []
    for run_number in range(1, args.repetitions + 1):
        concrete_result = measure(root, "57-concrete", concrete)
        concrete_result["run"] = run_number
        concrete_result["repetitions"] = args.repetitions
        concrete_results.append(concrete_result)
        print(json.dumps(concrete_result, ensure_ascii=False, sort_keys=True))
    concrete_summary = summarize_concrete_runs(
        concrete_results,
        expected_count=args.concrete_count,
    )
    gate_passed = passes_gate(
        concrete_summary,
        threshold_seconds=args.threshold_seconds,
    )
    print(json.dumps(concrete_summary, ensure_ascii=False, sort_keys=True))

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
                "statistic": "median_wall_seconds",
                "median_wall_seconds": concrete_summary["median_wall_seconds"],
                "max_wall_seconds": concrete_summary["max_wall_seconds"],
                "complete_runs": concrete_summary["complete_runs"],
                "required_complete_runs": concrete_summary[
                    "required_complete_runs"
                ],
                "passed": gate_passed,
            },
            sort_keys=True,
        )
    )
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
