"""Test instruments, not a local-model implementation or a permission engine.

Secrets are compared in memory. Neither HTTP transcripts nor assertion messages
contain their values. All listeners bind loopback on OS-assigned ports.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import ssl
import threading
import urllib.error
import urllib.request


MODEL = "fixture-glm-model"
MAIL = tuple("mcp__fixture_mail__" + name for name in (
    "fetch_inbox", "reserve_files", "renew_reservations",
    "release_reservations", "send_message",
))
TOOL_NAME = "fixture_echo"
PARAMETERS = {"type": "object", "properties": {"text": {"type": "string"}},
              "required": ["text"], "additionalProperties": False}
RESPONSE_TOOL = {"type": "function", "name": TOOL_NAME,
                 "description": "Echo the fixture text", "parameters": PARAMETERS}
MESSAGE_TOOL = {"name": TOOL_NAME, "description": "Echo the fixture text",
                "input_schema": PARAMETERS}
FIXTURE_ADDRESSES = set()


@contextmanager
def registered_address(address):
    assert address not in FIXTURE_ADDRESSES
    FIXTURE_ADDRESSES.add(address)
    try:
        yield
    finally:
        FIXTURE_ADDRESSES.remove(address)


class Canary:
    def __init__(self):
        self.__value = "fixture-secret-" + secrets.token_hex(24)

    def __repr__(self):
        return "<Canary redacted>"

    def header(self, kind):
        # Only the fake credential process and fake HTTP peer need this value.
        return "Bearer " + self.__value if kind == "bearer" else self.__value

    def matches(self, value, kind):
        return secrets.compare_digest(str(value), self.header(kind))

    def scan(self, surfaces):
        for name, value in surfaces.items():
            raw = value if isinstance(value, bytes) else json.dumps(value, default=str).encode()
            if self.__value.encode() in raw:
                raise AssertionError("credential exposed on " + name)


def canonical_digest(profile):
    """Independent specification oracle; never used as the product resolver."""
    raw = json.dumps(profile, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def profile_text(base_url="https://inference.example.invalid/v1"):
    return f'''schema_version = 1
name = "fixture"
model = "{MODEL}"
context_window = 131072
max_output_tokens = 16384

[codex]
base_url = "{base_url}"
wire_api = "responses"
env = {{}}

[claude]
base_url = "https://inference.example.invalid"
wire_api = "anthropic-messages"
env = {{ CLAUDE_AUTOCOMPACT_PCT_OVERRIDE = "85" }}
'''


@dataclass(repr=False)
class FakePeer:
    protocol: str
    canary: Canary
    auth: str = "none"
    fault: str | None = None
    requests: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    _server: object = None
    tool_name: str = TOOL_NAME
    ca_file: Path | None = None
    credential_epoch: int = 0

    def rotate_credential(self, canary):
        self.canary = canary
        self.credential_epoch += 1

    def __repr__(self):
        return f"<FakePeer {self.protocol} requests={len(self.requests)}>"

    @property
    def base_url(self):
        scheme = "https" if self.ca_file else "http"
        return f"{scheme}://127.0.0.1:{self._server.server_port}"

    def inspect(self, path, headers, body):
        expected_path = "/v1/responses" if self.protocol == "responses" else "/v1/messages"
        form = None
        if headers.get("Authorization", "").startswith("Bearer "):
            form = "bearer"
            auth_ok = self.canary.matches(headers["Authorization"], form)
        elif headers.get("X-Api-Key"):
            form = "x-api-key"
            auth_ok = self.canary.matches(headers["X-Api-Key"], form)
        else:
            auth_ok = self.auth == "none"
        auth_headers = [name for name in ("Authorization", "X-Api-Key") if headers.get(name)]
        if self.auth == "none":
            auth_ok = not auth_headers
        else:
            auth_ok = auth_ok and (form == self.auth or self.auth == "either") and len(auth_headers) == 1
        # Do not keep raw headers, including unexpected secret-bearing headers.
        self.canary.scan({"request-body": body})
        self.requests.append({"path": path, "body": body, "auth_form": form,
                              "auth_header_names": auth_headers, "auth_ok": auth_ok,
                              "credential_epoch": self.credential_epoch if auth_ok and form else None})
        if path != expected_path:
            self.violations.append("unexpected-path")
            return 404
        if not auth_ok:
            self.violations.append("auth-header-unsupported")
            return 401
        if body.get("model") != MODEL:
            self.violations.append("wrong-model")
            return 400
        tools = body.get("tools", [])
        # The fixture server has one exact flattened name, not an arbitrary
        # server prefix whose suffix happens to match. Native acceptance must
        # explicitly bind this fixture identity and still require exact schema.
        fixture_tools = [tool for tool in tools if tool.get("name") in (
            TOOL_NAME, "mcp__fixture__fixture_echo")]
        schema_key = "parameters" if self.protocol == "responses" else "input_schema"
        if len(fixture_tools) != 1 or fixture_tools[0].get(schema_key) != PARAMETERS:
            self.violations.append("function-tool-missing")
            return 400
        if self.protocol == "responses" and fixture_tools[0].get("type") != "function":
            self.violations.append("function-tool-missing")
            return 400
        self.tool_name = fixture_tools[0]["name"]
        if any(t.get("type") in ("code_mode", "code", "namespace") for t in tools):
            self.violations.append("non-function-tools")
            return 400
        if self.protocol == "messages" and not headers.get("Anthropic-Version"):
            self.violations.append("anthropic-version-missing")
            return 400
        return 200

    def payload(self, turn):
        if self.protocol == "responses":
            output = ([{"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
                        "name": self.tool_name, "arguments": '{"text":"fixture"}', "status": "completed"}]
                      if turn == 1 else
                      [{"type": "message", "id": "msg_fixture", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text", "text": "fixture done", "annotations": []}]}])
            return {"id": f"resp_fixture_{turn}", "object": "response", "created_at": 1,
                    "model": MODEL, "status": "completed", "output": output,
                    "error": None, "incomplete_details": None,
                    "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12}}
        content = ([{"type": "tool_use", "id": "call_fixture", "name": self.tool_name,
                     "input": {"text": "fixture"}}] if turn == 1 else
                   [{"type": "text", "text": "fixture done"}])
        return {"id": f"msg_fixture_{turn}", "type": "message", "role": "assistant",
                "model": MODEL, "content": content,
                "stop_reason": "tool_use" if turn == 1 else "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 8, "output_tokens": 4}}

    def events(self, payload):
        if self.protocol == "responses":
            start = {**payload, "status": "in_progress", "output": []}
            rows = [("response.created", {"response": start})]
            for i, item in enumerate(payload["output"]):
                initial = {**item}
                if item["type"] == "function_call":
                    initial.update(arguments="", status="in_progress")
                elif item["type"] == "message":
                    initial.update(content=[], status="in_progress")
                rows.append(("response.output_item.added", {"output_index": i, "item": initial}))
                if item["type"] == "function_call":
                    rows.extend([
                        ("response.function_call_arguments.delta", {"item_id": item["id"], "output_index": i, "delta": item["arguments"]}),
                        ("response.function_call_arguments.done", {"item_id": item["id"], "output_index": i, "arguments": item["arguments"]}),
                    ])
                else:
                    text = item["content"][0]
                    common = {"item_id": item["id"], "output_index": i, "content_index": 0}
                    rows.extend([
                        ("response.content_part.added", {**common, "part": {**text, "text": ""}}),
                        ("response.output_text.delta", {**common, "delta": text["text"]}),
                        ("response.output_text.done", {**common, "text": text["text"]}),
                        ("response.content_part.done", {**common, "part": text}),
                    ])
                rows.append(("response.output_item.done", {"output_index": i, "item": item}))
            rows.append(("response.completed", {"response": payload}))
            return [{"type": name, "sequence_number": n, **body} for n, (name, body) in enumerate(rows)]
        rows = [{"type": "message_start", "message": {**payload, "content": [], "stop_reason": None}}]
        for i, block in enumerate(payload["content"]):
            initial = {**block, **({"input": {}} if block["type"] == "tool_use" else {"text": ""})}
            delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                     if block["type"] == "tool_use" else {"type": "text_delta", "text": block["text"]})
            rows += [{"type": "content_block_start", "index": i, "content_block": initial},
                     {"type": "content_block_delta", "index": i, "delta": delta},
                     {"type": "content_block_stop", "index": i}]
        return rows + [{"type": "message_delta", "delta": {"stop_reason": payload["stop_reason"], "stop_sequence": None}, "usage": payload["usage"]}, {"type": "message_stop"}]


def fixture_tls(directory, cert_ip="127.0.0.1"):
    """Ephemeral fixture CA/key, unrelated to credential canaries or real keys."""
    from datetime import datetime, timedelta, timezone
    from ipaddress import ip_address
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    directory.mkdir(mode=0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture-loopback")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address(cert_ip))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    ca_file, key_file = directory / "ca.pem", directory / "key.pem"
    ca_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_file.chmod(0o600)
    return ca_file, key_file


@contextmanager
def fake_peer(protocol, canary, auth="none", fault=None, tls_dir=None, cert_ip="127.0.0.1"):
    peer = FakePeer(protocol, canary, auth, fault)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            status = peer.inspect(self.path, self.headers, body)
            if status == 200 and peer.fault in ("401", "500"):
                status = int(peer.fault)
            peer.requests[-1]["http_status"] = status
            if status == 200 and peer.fault == "redirect":
                peer.requests[-1]["http_status"] = 307
                self.send_response(307)
                self.send_header("Location", peer.redirect_url)
                self.end_headers()
                return
            if status == 200 and peer.fault == "disconnect":
                peer.requests[-1]["http_status"] = None
                self.close_connection = True
                return
            self.send_response(status)
            streaming = body.get("stream", False) and status == 200
            self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
            self.end_headers()
            if status != 200:
                self.wfile.write(b'{"error":{"type":"fixture_rejection","message":"fixture rejected"}}')
                return
            payload = peer.payload(len(peer.requests))
            if streaming:
                for event in peer.events(payload):
                    self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
            else:
                self.wfile.write(json.dumps(payload).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls_dir is not None:
        peer.ca_file, key_file = fixture_tls(tls_dir, cert_ip)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(peer.ca_file, key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.daemon_threads = True
    peer._server = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    with registered_address(("127.0.0.1", server.server_port)):
        thread.start()
        try:
            yield peer
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            assert not thread.is_alive(), "fixture HTTP server did not stop"


def post_fixture(peer, body, auth=None, path=None):
    """Only an instrument self-test client, NOT a stand-in for either CLI."""
    path = path or ("/v1/responses" if peer.protocol == "responses" else "/v1/messages")
    headers = {"Content-Type": "application/json", "Anthropic-Version": "2023-06-01"}
    if auth:
        headers["Authorization" if auth == "bearer" else "X-Api-Key"] = peer.canary.header(auth)
    request = urllib.request.Request(peer.base_url + path, json.dumps(body).encode(), headers)
    # Ignore the operator's proxy env, including for loopback. No redirect replay.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_):
            return None
    handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
    if peer.ca_file:
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=peer.ca_file)))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=2) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def assert_roundtrip(peer):
    assert len(peer.requests) >= 2, "two provider requests required"
    assert not peer.violations, "wire protocol rejected"
    second = peer.requests[1]["body"]
    if peer.protocol == "responses":
        items = second.get("input", [])
        assert any(row.get("type") == "function_call" and row.get("call_id") == "call_fixture" for row in items), "function call replay missing"
        assert any(row.get("type") == "function_call_output" and row.get("call_id") == "call_fixture" and row.get("output") == "fixture" for row in items), "function result replay missing"
    else:
        messages = second.get("messages", [])
        assert any(row.get("role") == "assistant" and any(block.get("type") == "tool_use" and block.get("id") == "call_fixture" for block in row.get("content", []) if isinstance(block, dict)) for row in messages), "tool_use replay missing"
        assert any(row.get("role") == "user" and any(block.get("type") == "tool_result" and block.get("tool_use_id") == "call_fixture" and block.get("content") == "fixture" for block in row.get("content", []) if isinstance(block, dict)) for row in messages), "tool_result replay missing"


def assert_same_launch(plans):
    assert len(plans) >= 3, "spawn, resume and wake observations required"
    # Provider-native session/resume CLI flags may differ. Compare the common
    # saved execution projection and retain the original native session ID.
    keys = ("intent_digest", "profile_sha256", "model", "base_url", "context_window",
            "max_output_tokens", "provider_session_id", "execution", "auth_plan")
    baseline = {key: plans[0][key] for key in keys}
    for plan in plans[1:]:
        assert {key: plan[key] for key in keys} == baseline, "saved launch intent drift"


def assert_auth_context(call, plan):
    assert call["argv"] == plan["argv"], "credential argv changed"
    assert call["cwd"] == plan["cwd"], "credential cwd changed"
    assert call["env"] == plan["env"], "credential environment changed"
    assert call.get("shell", False) is False, "credential command shell evaluated"
    assert call["timeout"] == plan["timeout_ms"] / 1000, "credential timeout changed"


def assert_delegation(trace, allowed, edited_path):
    assert trace["permission_mode"] == "dontAsk", "unattended permission mode changed"
    assert trace["effective_scope"] == allowed, "effective scope widened"
    assert trace["human_prompts"] == 0, "unattended child prompted"
    assert trace["reservation_hook_enabled"], "reservation hook bypassed"
    expected = [MAIL[0], "Read", MAIL[1], "Edit", MAIL[2], MAIL[3], MAIL[4]]
    assert [row["tool"] for row in trace["events"]] == expected, "delegation tool order changed"
    assert trace["events"][3]["path"] == str(edited_path), "wrong edit target"
    assert trace["events"][3]["reservation_held"], "edit without reservation"
    assert trace["active_reservations"] == [], "reservation not released"


def assert_denial(trace):
    assert trace["denied"] and trace["human_prompts"] == 0, "scope denial did not finish unattended"
    assert trace["unapproved_reads"] == [] and trace["unapproved_writes"] == [], "scope side effect"
    assert trace["active_reservations"] == [], "reservation not released after denial"
    assert trace["events"][-1]["tool"] == MAIL[4], "denial not reported through Mail"


def assert_failure(trace, reason, pre_exec):
    assert trace["reason"] == reason, "wrong fixed failure reason"
    assert trace["cloud_requests"] == 0 and trace["other_origin_requests"] == 0, "endpoint fallback"
    assert not trace["model_success"], "request failure reported as model success"
    assert trace["exec_observed"] is not pre_exec, "exec receipt phase lost"
    if pre_exec:
        assert trace["effects"] == [], "structural rejection produced side effects"
    else:
        assert trace["phase"] != "pre-exec", "post-exec failure relabelled pre-exec"
