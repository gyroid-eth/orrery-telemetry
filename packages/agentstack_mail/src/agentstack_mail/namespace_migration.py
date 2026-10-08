"""Candidate-only DB/state rehearsal with durable, resumable cutover receipts.

No discovery, installed pointer, environment switch or service management.
The caller supplies a trusted fence collector; PR4/7 wire real writer control.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .namespace_plan import (
    SourceSnapshot,
    MigrationReceipt,
    FinalValidationEvidence,
    FenceEvidence,
    FINAL_CHECKS,
    fingerprint,
)
from .namespace_state_io import (
    SourceBundle,
    StateMigrationError,
    atomic_json,
    connection,
    tree_manifest,
)
from .namespace_transform import resolve_mail, convert_bundle, validate_bundle

PHASES = (
    "planned",
    "prepared",
    "prevalidated",
    "quiesced",
    "final_verified",
    "switched",
    "committed",
)


def bundle_at(root: Path) -> SourceBundle:
    return SourceBundle(
        root / "mail.sqlite3",
        root / "delivery.sqlite3",
        root / "archive",
        root / "signals",
        root / "history",
        root / "bindings.json",
        root / "legacy-config.json",
    )


def seal(document: dict) -> dict:
    return {"payload": document, "integrity": fingerprint(document)}


def unseal(path: Path) -> dict:
    try:
        document = json.loads(path.read_text())
        if set(document) != {"payload", "integrity"} or document[
            "integrity"
        ] != fingerprint(document["payload"]):
            raise ValueError
        return document["payload"]
    except (KeyError, TypeError, ValueError):
        raise StateMigrationError("RECEIPT_INVALID") from None


class NamespaceMigration:
    """Owned workspace only. Switching rehearses a local pointer, activation stays false."""

    def __init__(self, source: SourceBundle, workspace: Path, choices: dict):
        self.source = source
        if workspace.is_symlink():
            raise StateMigrationError("WORKSPACE_SYMLINK")
        self.workspace = workspace.resolve()
        self.choices = json.loads(json.dumps(choices, allow_nan=False))
        for path in asdict(source).values():
            resolved = Path(path).resolve()
            if (
                Path(path).is_symlink()
                or resolved == self.workspace
                or resolved in self.workspace.parents
                or self.workspace in resolved.parents
            ):
                raise StateMigrationError("SOURCE_WORKSPACE_OVERLAP")
        config = json.loads(source.config.read_text())
        self.writer_roster = tuple(config.get("expected_writers", ()))
        # PR1 validates nonempty/unique roster, and explicit block scope is required.
        if config.get("instruction_blocks") != []:
            raise StateMigrationError("BLOCK_COLLECTOR_REQUIRED")
        owner = self.workspace / "workspace-owner.json"
        expected_owner = {
            "format": "namespace-candidate-workspace",
            "source_paths": {
                key: str(value.resolve()) for key, value in asdict(source).items()
            },
        }
        if self.workspace.exists() and any(self.workspace.iterdir()):
            if (
                not owner.is_file()
                or owner.is_symlink()
                or json.loads(owner.read_text()) != expected_owner
            ):
                raise StateMigrationError("WORKSPACE_NOT_OWNED")
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace.chmod(0o700)
        if not owner.exists():
            atomic_json(owner, expected_owner)
        self.receipt_path = self.workspace / "receipt.json"
        self.pointer_path = self.workspace / "candidate-pointer.json"

    @contextmanager
    def _locked(self):
        descriptor = os.open(
            self.workspace / "migration.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateMigrationError("MIGRATION_BUSY") from None
            yield
        finally:
            os.close(descriptor)

    def _snapshot(self) -> SourceSnapshot:
        from .namespace_plan import validate_roster

        current = json.loads(self.source.config.read_text())
        if validate_roster(current.get("expected_writers")) != tuple(
            sorted(self.writer_roster)
        ):
            raise StateMigrationError("WRITER_ROSTER_CHANGED")
        if current.get("instruction_blocks") != []:
            raise StateMigrationError("BLOCK_COLLECTOR_REQUIRED")
        manifest = self.source.manifest()
        return SourceSnapshot.capture(
            fingerprint(manifest),
            {"bundle": manifest, "expected_writers": self.writer_roster},
        )

    def _save(self, receipt: dict) -> None:
        atomic_json(self.receipt_path, seal(receipt))

    def _load(self) -> dict:
        receipt = unseal(self.receipt_path)
        if (
            receipt.get("version") != 1
            or receipt.get("phase") not in PHASES
            or receipt.get("activation") is not False
            or receipt.get("choices_digest") != fingerprint(self.choices)
            or receipt.get("source_paths")
            != {key: str(value.resolve()) for key, value in asdict(self.source).items()}
        ):
            raise StateMigrationError("PLAN_CHANGED")
        planner = MigrationReceipt.from_json(receipt["planner"])
        if planner.writer_roster != tuple(sorted(self.writer_roster)) or receipt[
            "writer_roster"
        ] != list(planner.writer_roster):
            raise StateMigrationError("WRITER_ROSTER_CHANGED")
        return receipt

    def _candidate_manifest(self, receipt: dict) -> dict:
        root = self.workspace / "candidate"
        return {
            "bundle": bundle_at(root).manifest(),
            "resources": tree_manifest(root),
            "mapping": fingerprint(json.loads((root / "mapping.json").read_text())),
            "resolutions_digest": fingerprint(
                json.loads(
                    MigrationReceipt.from_json(receipt["planner"]).resolutions_json
                )
            ),
            "activation": False,
        }

    def _build(self, snapshot: SourceBundle, receipt: dict, generation: str) -> dict:
        resolved = resolve_mail(
            snapshot, self.choices, prior_windows=receipt["windows"]
        )
        resolved["migration_generation"] = fingerprint(
            {
                "receipt_id": MigrationReceipt.from_json(receipt["planner"]).receipt_id,
                "source_generation": generation,
            }
        )
        candidate = self.workspace / "candidate"
        if candidate.exists():
            shutil.rmtree(candidate)
        for name in ("archive", "signals", "history"):
            (candidate / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        convert_bundle(snapshot, candidate, resolved, self.choices)
        return validate_bundle(snapshot, candidate, resolved, self.choices)

    def _fence(self, supplied, snapshot, receipt):
        fence = (
            supplied(snapshot.generation, tuple(receipt["writer_roster"]))
            if callable(supplied)
            else supplied
        )
        if not isinstance(fence, FenceEvidence) or not fence.ready(
            snapshot.generation, tuple(receipt["writer_roster"])
        ):
            raise StateMigrationError("WRITER_FENCE_UNCONFIRMED")
        return fence

    def resume(
        self,
        *,
        fence: FenceEvidence | Callable,
        fault: Callable[[str, str], None] | None = None,
    ) -> dict:
        fault = fault or (lambda phase, point: None)
        with self._locked():
            if not self.receipt_path.exists():
                fault("planned", "before")
                snapshot = self._snapshot()
                resolved = resolve_mail(
                    self.source,
                    self.choices,
                    prior_windows=self.choices.get("window_targets"),
                )
                resolutions = {
                    **self.choices,
                    "window_targets": resolved["windows"],
                    "unresolved": [],
                }
                planner = MigrationReceipt.create(snapshot, resolutions)
                receipt = {
                    "version": 1,
                    "activation": False,
                    "phase": "planned",
                    "choices_digest": fingerprint(self.choices),
                    "windows": resolved["windows"],
                    "writer_roster": list(planner.writer_roster),
                    "source_paths": {
                        key: str(value.resolve())
                        for key, value in asdict(self.source).items()
                    },
                    "planner": planner.to_json(),
                    "initial_generation": snapshot.generation,
                    "report": {},
                    "final_manifest": None,
                }
                self._save(receipt)
                fault("planned", "after")
            receipt = self._load()
            if receipt["phase"] == "committed":
                self._check_pointer(receipt, receipt["final_generation"])
            while receipt["phase"] != "committed":
                phase = PHASES[PHASES.index(receipt["phase"]) + 1]
                fault(phase, "before")
                planner = MigrationReceipt.from_json(receipt["planner"])
                if phase == "prepared":
                    if self._snapshot().generation != receipt["initial_generation"]:
                        raise StateMigrationError("PLAN_INVALIDATED")
                    self.source.snapshot(self.workspace / "initial")
                    planner = planner.advance("prepared")
                elif phase == "prevalidated":
                    initial = self.workspace / "initial"
                    snapshot = SourceBundle(
                        initial / "mail.sqlite3",
                        initial / "delivery.sqlite3",
                        initial / "archive",
                        initial / "signals",
                        initial / "history",
                        initial / "bindings.json",
                        initial / "config.json",
                    )
                    receipt["report"] = self._build(
                        snapshot, receipt, receipt["initial_generation"]
                    )
                    planner = planner.advance("prevalidated")
                elif phase == "quiesced":
                    snapshot = self._snapshot()
                    evidence = self._fence(fence, snapshot, receipt)
                    planner = planner.quiesce(evidence)
                elif phase == "final_verified":
                    snapshot = self._snapshot()
                    writer_evidence = self._fence(fence, snapshot, receipt)
                    final = self.source.snapshot(self.workspace / "final")
                    receipt["report"] = self._build(final, receipt, snapshot.generation)
                    if self._snapshot() != snapshot:
                        raise StateMigrationError("FINAL_SOURCE_CHANGED")
                    receipt["planner"] = planner.to_json()
                    manifest = self._candidate_manifest(receipt)
                    evidence = FinalValidationEvidence.record(
                        snapshot, manifest, {key: "pass" for key in FINAL_CHECKS}
                    )
                    planner = planner.verify_final(
                        snapshot, manifest, evidence, writer_evidence
                    )
                    receipt["final_manifest"] = manifest
                    receipt["final_generation"] = snapshot.generation
                elif phase in ("switched", "committed"):
                    snapshot = self._snapshot()
                    evidence = self._fence(fence, snapshot, receipt)
                    manifest = self._candidate_manifest(receipt)
                    gate = planner.gate(snapshot, manifest, evidence)
                    if not gate.ready or gate.activation_enabled is not False:
                        raise StateMigrationError("FINAL_GATE_REFUSED")
                    if phase == "switched":
                        atomic_json(
                            self.pointer_path,
                            {
                                "authority": "candidate-rehearsal",
                                "generation": snapshot.generation,
                                "receipt_id": planner.receipt_id,
                                "activation": False,
                            },
                        )
                        fault("switch_pointer", "after")
                    else:
                        self._check_pointer(receipt, snapshot.generation)
                receipt.update(phase=phase, planner=planner.to_json())
                self._save(receipt)
                fault(phase, "after")
            return self.status()

    def _check_pointer(self, receipt: dict, generation: str) -> None:
        expected = {
            "authority": "candidate-rehearsal",
            "generation": generation,
            "receipt_id": MigrationReceipt.from_json(receipt["planner"]).receipt_id,
            "activation": False,
        }
        try:
            valid = json.loads(self.pointer_path.read_text()) == expected
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise StateMigrationError("POINTER_MISMATCH")

    def status(self) -> dict:
        if not self.receipt_path.exists():
            return {
                "activation": False,
                "phase": "unplanned",
                "rollback_allowed": False,
            }
        receipt = self._load()
        rollback = receipt["phase"] not in (
            "planned",
            "prepared",
            "prevalidated",
            "quiesced",
            "final_verified",
        )
        if rollback:
            try:
                rollback = (
                    self._candidate_manifest(receipt) == receipt["final_manifest"]
                )
            except (OSError, ValueError):
                rollback = False
        return {
            "activation": False,
            "rehearsal": True,
            "phase": receipt["phase"],
            "receipt_id": MigrationReceipt.from_json(receipt["planner"]).receipt_id,
            "generation": receipt.get("final_generation"),
            "rollback_allowed": rollback,
            "report": receipt["report"],
            "rollback_reason": (
                "candidate_unchanged" if rollback else "forward_fix_or_export_required"
            ),
        }

    def rollback(self) -> dict:
        with self._locked():
            receipt = self._load()
            if not self.status()["rollback_allowed"]:
                raise StateMigrationError("ROLLBACK_AFTER_NEW_WRITES_REFUSED")
            atomic_json(
                self.pointer_path,
                {"authority": "legacy-rehearsal", "activation": False},
            )
            # Keep resolution/UUID decisions, return to the initial snapshot's verification.
            planner = MigrationReceipt.from_json(receipt["planner"])
            from dataclasses import replace

            planner = replace(
                planner,
                phase="prepared",
                final_source_digest=None,
                final_generation=None,
                final_candidate_digest=None,
                final_checks=(),
            )
            receipt.update(
                phase="prepared", planner=planner.to_json(), final_manifest=None
            )
            self._save(receipt)
            return self.status()
