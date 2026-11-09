"""Independent file/wire/digest instruments added after the first review.

No product resolver, permission parser, native CLI or synthetic acceptance result
is implemented here. Mutant controls exercise only these measurement oracles.
"""
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import sys
import threading
import uuid

from .support import canonical_digest, registered_address


def snapshot_tree(root):
    """Names + byte digests detect writes on BOTH sides of a compound command."""
    result = {}
    for path in root.rglob("*"):
        stat = path.lstat()
        metadata = (stat.st_mode, stat.st_uid)
        if path.is_symlink():
            value = ("symlink", str(path.readlink()), metadata)
        elif path.is_dir():
            value = ("directory", metadata)
        elif path.is_file():
            value = ("file", hashlib.sha256(path.read_bytes()).hexdigest(), metadata)
        else:
            value = ("special", metadata)
        result[str(path.relative_to(root))] = value
    return result


def assert_tree_unchanged(root, before):
    assert snapshot_tree(root) == before, "protected file tree changed"


def assert_auth_matrix(observations, source):
    assert {peer.auth for peer, _ in observations} == {"bearer", "x-api-key"}, "incomplete auth matrix"
    versions = {trace["cli_version"] for _, trace in observations}
    assert len(versions) == 1, "different native CLI versions in header matrix"
    successful = []
    for peer, trace in observations:
        assert peer.requests, "no independently observed auth request"
        if trace["model_success"]:
            assert all(row["auth_ok"] and row["auth_form"] == peer.auth for row in peer.requests), "false auth success"
            assert not peer.violations and all(row["http_status"] == 200 for row in peer.requests), "peer did not accept authenticated model request"
            successful.append(peer.auth)
        else:
            assert trace["reason"] == "LOCAL_MODEL_AUTH_HEADER_UNSUPPORTED", "unexpected auth rejection"
    assert successful, "native authentication must succeed in at least one header shape"
    assert source.issued, "native credential helper was never called"
    return successful


class CredentialSource:
    """Fixture credential service: native credential subprocess fetches a pipe value.

    The service, not an adapter's trace, counts actual credential retrievals.
    No token is placed in a file/env/argv or in the retrieval observations.
    """
    def __init__(self, canary):
        self.current = canary
        self.epoch = 0
        self.issued = []
        self.server = None

    def __repr__(self):
        return "<CredentialSource redacted>"

    def rotate(self, canary):
        self.current = canary
        self.epoch += 1

    def helper_argv(self, path):
        # URL contains no credential. A later native binding must approve this
        # absolute script as the auth command; it must not fetch on CLI's behalf.
        url = f"http://127.0.0.1:{self.server.server_port}/credential"
        path.write_text("import sys, urllib.request\n"
                        "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
                        f"with opener.open({url!r}, timeout=2) as response:\n"
                        "    sys.stdout.buffer.write(response.read())\n")
        path.chmod(0o500)
        return [sys.executable, str(path)]


@contextmanager
def credential_source(canary):
    source = CredentialSource(canary)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path != "/credential":
                self.send_error(404)
                return
            source.issued.append(source.epoch)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(source.current.header("x-api-key").encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    source.server = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    with registered_address(("127.0.0.1", server.server_port)):
        thread.start()
        try:
            yield source
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            assert not thread.is_alive()


def assert_wire_rotation(peer, source):
    assert source.epoch == peer.credential_epoch == 1, "credential rotation not exercised"
    assert 0 in source.issued and 1 in source.issued, "native helper did not retrieve both credentials"
    accepted = [row for row in peer.requests if row["auth_ok"]]
    epochs = [row["credential_epoch"] for row in accepted]
    assert 0 in epochs and 1 in epochs and epochs.index(0) < epochs.index(1), "peer did not accept old then new credential"
    assert not any(not row["auth_ok"] for row in peer.requests), "stale or missing credential on wire"
    assert not peer.violations and all(row["http_status"] == 200 for row in peer.requests), "credential accepted but model request rejected"
    assert len({row["auth_form"] for row in accepted}) == 1, "native auth mechanism changed during rotation"


def owner_paths(base, mode):
    # Existing global CLIENT_LAYOUT; legacy hooks/child_resume.py:_paths.
    if mode == "global":
        owner = base / "profile.json"
        return owner, base / "runtime/launch.json", base / "runtime/mutation-pending.json"
    owner = base / "child-agents/FixtureChild.json"
    # Legacy owns the one LaunchIntent in child-state; global launch/receipt
    # slots are NOT newly introduced into legacy. Pending migration is existing.
    return owner, None, base / "child-agents/.FixtureChild.legacy-migration.json"


def write_private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, sort_keys=True))
    path.chmod(0o600)


def existing_mutation_pending():
    """Use the existing S2a normalizer, not a guessed pending request schema."""
    root = Path(__file__).resolve().parents[2]
    api = runpy.run_path(str(root / "bin/lib/runtime_client.py"))
    contract = api["wire_contract"]()
    tool = "refresh_registration"
    declaration = contract["fixture"]["tool_contracts"][tool]
    args = {"expected_server_instance_id": "fixture-instance", "candidate_generation": "fixture-generation",
            "authority_epoch": "fixture-epoch", "agent_id": 42,
            "registration_token": "fixture-placeholder-not-a-credential",
            "expected_credential_generation": 1, "task_description": "fixture",
            "request_id": "00000000-0000-4000-8000-000000000042"}
    values = contract["normalize_arguments"](args, declaration["input_schema"])
    raw, hashed = contract["request_preimage"](tool, values, declaration["accepted_ignored_arguments"])
    return {"kind": "orrery-global-operation-pending-v1", "tool": tool,
            "request_id": args["request_id"], "server_instance_id": args["expected_server_instance_id"],
            "candidate_generation": args["candidate_generation"], "authority_epoch": args["authority_epoch"],
            "agent_id": 42, "credential_generation": 1, "canonical_payload": json.loads(raw),
            "request_hash": hashed, "phase": "planned", "receipt": None}


def contains_full_intent(value):
    if isinstance(value, dict):
        model = value.get("model")
        if isinstance(model, dict) and model.get("kind") == "local-profile" and "resolved" in model:
            return True
        return any(contains_full_intent(child) for child in value.values())
    return isinstance(value, list) and any(contains_full_intent(child) for child in value)


def assert_single_owner(base, owner):
    # Discover unexpected files as well as inspecting known profile/child/launch
    # paths; naming a duplicate 'cache.json' must not evade the check.
    copies = []
    for path in base.rglob("*.json"):
        assert not path.is_symlink(), "unexpected symlink in intent storage"
        if contains_full_intent(json.loads(path.read_text())):
            copies.append(path)
    assert copies == [owner], "full LaunchIntent has a shadow owner"


def intent_preimage(intent):
    # Explicit contract: only the digest field itself is excluded. Therefore
    # normalized model.resolved auth/runner and effective_scope are covered, not
    # merely a product-selected subset or a product-generated digest string.
    return {key: value for key, value in intent.items() if key != "intent_digest"}


def assert_intent_digest(intent, actual):
    assert actual == canonical_digest(intent_preimage(intent)), "LaunchIntent digest preimage mismatch"


def assert_bound_receipt(intent, receipt):
    expected = canonical_digest(intent_preimage(intent))
    assert receipt["intent_digest"] == expected, "receipt not bound to full execution intent"


def literal_intent(workspace):
    """A fixed preimage for hashing tests only; NEVER resolves a caller/profile."""
    import tomllib
    profile = tomllib.loads(workspace.profile.read_text())
    return {"schema_version": 1, "revision": 1, "provider": "claude",
            "model": {"kind": "local-profile", "ref": "local:fixture",
                      "profile_sha256": canonical_digest(profile),
                      "resolved": {"model": profile["model"], "context_window": 131072,
                                   "max_output_tokens": 16384, "adapter": profile["claude"],
                                   "auth": {"context": deepcopy(workspace.context["auth_context"]),
                                            "runner": deepcopy(workspace.context["auth_runner"])}}},
            "cwd": str(workspace.scratch.parent), "task": str(workspace.task),
            "effective_scope": deepcopy(workspace.scope)}


CALLERS = {
    "shell": "hooks/spawn_child.sh",
    "prepare-child": "RuntimeClient.prepare_child",
    "resume-child": "RuntimeClient.resume_child",
    "operator": "RuntimeOperator.spawn",
    "dashboard": "dashboard.server.do_spawn",
}


def capture_provider(path, capture, *, nonce=None):
    if path.exists():
        assert not path.is_symlink(), "capture binary must not be a symlink"
        path.chmod(0o600)
    path.write_text("#!" + sys.executable + "\nimport json, os, sys\n"
                    "from pathlib import Path\n"
                    f"Path({str(capture)!r}).write_text(json.dumps({{\"argv\":sys.argv[1:],\"env\":dict(os.environ),\"cwd\":os.getcwd(),\"invocation_nonce\":{nonce!r}}}))\n")
    path.chmod(0o500)
    return path


def prepare_caller_invocation(workspace, entry, *, resume_owner=None):
    """Reset only this fixture's client; prior resume state is explicit INPUT.

    Capture artifacts live outside that client. Keeping the previous capture
    makes the no-op control prove it cannot stand in for the current execution.
    The nonce is embedded in the command, never in its argv/environment.
    """
    assert entry in CALLERS
    base = workspace.tmp / "client"
    assert not base.is_symlink(), "fixture client must not be a symlink"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(mode=0o700)
    owner, _, _ = owner_paths(base, "global")
    assert (resume_owner is not None) == (entry == "resume-child")
    if resume_owner is not None:
        owner.write_bytes(resume_owner)
        owner.chmod(0o600)
    assert owner.exists() == (entry == "resume-child")
    nonce = uuid.uuid4().hex
    captures = workspace.tmp / "captures"
    captures.mkdir(mode=0o700, exist_ok=True)
    capture = captures / f"{entry}-{nonce}.json"
    assert not capture.exists()
    binary = capture_provider(workspace.tmp / "bin/fixture-provider", capture, nonce=nonce)
    return owner, binary, capture, nonce


def read_fresh_capture(capture, nonce):
    assert capture.is_file() and not capture.is_symlink(), "current caller did not execute capture binary"
    observed = json.loads(capture.read_text())
    assert observed["invocation_nonce"] == nonce, "capture belongs to an earlier caller invocation"
    return observed


# Exact fixture task-delivery/tool instruction, not an adapter-provided trace.
CALLER_STARTUP_PROMPT = (
    "Your delegated task is in ORRERY Mail. Fetch your inbox and read the task before beginning work.\n\n"
    "Tools: this child was started with base mail-only: besides shell/files and authenticated ORRERY Mail "
    "it was given only nothing else. If a tool you need is missing, report it to your parent agent "
    "instead of working around it. This is an instruction, not a technical lock."
)


def assert_resume_capture(observed, startup, *, startup_prompt, envelope_index,
                          session_id, headless=False):
    """Remove ONLY the declared prompt and exact, position-bound envelope."""
    start = startup["argv"]
    assert start and start[-1] == startup_prompt, "unexpected startup prompt"
    common = start[:-1]
    envelope = (["exec"] if headless else []) + ["resume", session_id]
    argv = observed["argv"]
    assert 0 <= envelope_index <= len(common), "invalid resume envelope position"
    assert argv[envelope_index:envelope_index + len(envelope)] == envelope, "invalid resume envelope"
    remaining = argv[:envelope_index] + argv[envelope_index + len(envelope):]
    assert remaining == common, "resume changed full provider argv"
    assert observed["env"] == startup["env"], "resume changed provider env"
    assert observed["cwd"] == startup["cwd"], "resume changed provider cwd"


def assert_caller_capture(capture, expected):
    observed = json.loads(capture.read_text())
    assert observed["argv"] == expected["argv"], "caller bypass changed provider argv"
    assert observed["env"] == expected["env"], "caller bypass changed provider env"
    assert observed["cwd"] == expected["cwd"], "caller bypass changed provider cwd"
