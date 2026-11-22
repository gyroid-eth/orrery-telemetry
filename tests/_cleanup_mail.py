"""Explicit local transport for tests of successful cleanup and retention.

The optional callback bridges the subprocess to the bundled in-process Mail
service. Without it this is a synthetic Mail server, never delivery evidence.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


@contextmanager
def cleanup_mail_server(call=None):
    retired = {}

    def synthetic(method, arguments):
        name = arguments.get("agent_name")
        if method == "release_file_reservations":
            data = {"released": 0}
        elif method == "retire_agent":
            retired[name] = "2026-01-01T00:00:00+00:00"
            data = {"status": "retired", "agent_name": name,
                    "project_key": arguments.get("project_key")}
        elif method == "whois":
            data = {"name": name, "retired_at": retired.get(name)}
        else:
            return {"ok": False, "error": "unexpected cleanup RPC"}
        return {"ok": True, "data": data}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            params = request["params"]
            response = (call or synthetic)(params["name"], params["arguments"])
            result = ({"structuredContent": response["data"]} if response["ok"]
                      else {"isError": True, "content": [{"type": "text", "text": response.get("error", "RPC failed")}]})
            body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/mcp"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
