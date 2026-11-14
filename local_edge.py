"""Local edge. Stands in for an ALB: the page, the API, and the path to the door."""

import http.client
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import netutil
import telemetry

ROOT = Path(__file__).resolve().parent
DOOR_PORT = 8788

FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/fixtures/idle.json": ("fixtures/idle.json", "application/json"),
    "/fixtures/busy.json": ("fixtures/busy.json", "application/json"),
    "/page-load-note.txt": ("page-load-note.txt", "text/plain; charset=utf-8"),
    "/serving": ("serving.html", "text/html; charset=utf-8"),
    "/serving.html": ("serving.html", "text/html; charset=utf-8"),
    "/serving.js": ("serving.js", "text/javascript; charset=utf-8"),
}


class EdgeHandler(BaseHTTPRequestHandler):
    server_version = "local-edge"

    def setup(self):
        super().setup()
        netutil.enable_nodelay(self.connection)

    def log_message(self, fmt, *args):
        print("[local edge] " + (fmt % args))

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"service": "local edge", "ok": True})
            return
        if path == "/api/state":
            self._send_json(200, telemetry.STATE.snapshot())
            return
        if path == "/api/events":
            self._send_json(200, telemetry.STATE.public_events())
            return
        if path == "/api/traces":
            self._send_json(200, telemetry.STATE.public_traces())
            return
        if path.startswith("/api/traces/"):
            trace = telemetry.STATE.public_trace(path[len("/api/traces/"):])
            if trace is None:
                self._send_json(404, {"error": {"message": "Trace not found", "type": "not_found"}})
            else:
                self._send_json(200, trace)
            return
        if path == "/api/ready" and method == "POST":
            self._set_ready()
            return
        if path == "/api/hold-budget" and method == "POST":
            self._set_hold()
            return
        if path == "/api/budget" and method == "GET":
            self._budget()
            return
        if path == "/api/serving" and method == "GET":
            self._send_json(200, telemetry.STATE.serving())
            return
        if path == "/api/report" and method == "POST":
            self._store_report()
            return
        if path == "/api/spot-reclaim" and method == "POST":
            _discard_body(self)
            ready = telemetry.STATE.spot_reclaim()
            self._send_json(200, {"readyReplicas": ready})
            return
        if path == "/v1/chat/completions" and method == "POST":
            self._proxy_chat()
            return
        if path.startswith("/v1/"):
            self._proxy_plain(method, path)
            return
        if method == "GET":
            self._static(path)
            return
        if method == "POST":
            _discard_body(self)
        self._send_json(404, {"error": {"message": "Not found", "type": "not_found"}})

    def _set_ready(self):
        try:
            payload = _read_json(self)
            ready = payload.get("readyReplicas")
            if isinstance(ready, bool) or not isinstance(ready, int):
                raise ValueError("ready")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send_json(400, {"error": {"message": "readyReplicas must be an integer", "type": "bad_request"}})
            return
        applied = telemetry.STATE.set_ready(ready)
        self._send_json(200, {"readyReplicas": applied})

    def _set_hold(self):
        try:
            payload = _read_json(self)
            seconds = payload.get("seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                raise ValueError("seconds")
            applied = telemetry.STATE.set_hold_budget(seconds)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send_json(400, {"error": {"message": "seconds must be a number", "type": "bad_request"}})
            return
        self._send_json(200, {"seconds": applied})

    def _budget(self):
        caller, auth_error = telemetry.STATE.authorize(self.headers.get("Authorization"))
        if auth_error:
            self._send_json(401, {"error": {"message": "Unknown fixture credential", "type": "authentication_error"}})
            return
        found = telemetry.STATE.budget_remaining(caller)
        if found is None:
            self._send_json(401, {"error": {"message": "Unknown fixture credential", "type": "authentication_error"}})
            return
        remaining, reserved = found
        self._send_json(200, {"remaining": remaining, "reserved": reserved})

    def _store_report(self):
        try:
            payload = _read_json(self)
            stored = telemetry.STATE.store_report(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send_json(400, {"error": {"message": "Report was not stored", "type": "bad_request"}})
            return
        self._send_json(200, stored)

    def _proxy_chat(self):
        trace_id = uuid.uuid4().hex
        telemetry.STATE.begin_trace(trace_id)
        start = telemetry.STATE.now_ms()
        sent = False
        conn = http.client.HTTPConnection("127.0.0.1", DOOR_PORT, timeout=3600)
        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b""
            headers = {
                "content-type": self.headers.get("Content-Type") or "application/json",
                "content-length": str(len(raw)),
                "x-trace-id": trace_id,
            }
            authorization = self.headers.get("Authorization")
            if authorization:
                headers["authorization"] = authorization
            netutil.connect(conn)
            conn.request("POST", "/v1/chat/completions", body=raw, headers=headers)
            del raw
            resp = conn.getresponse()
            sent = True
            netutil.relay(self, resp, trace_id)
        except OSError:
            if not sent:
                self._send_json(502, {"error": {"message": "Local edge could not reach the door", "type": "edge_error"}})
        finally:
            conn.close()
            telemetry.STATE.end_span(trace_id, "local edge", start, telemetry.STATE.now_ms())

    def _proxy_plain(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", DOOR_PORT, timeout=30)
        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b""
            headers = {}
            if raw:
                headers["content-type"] = self.headers.get("Content-Type") or "application/json"
                headers["content-length"] = str(len(raw))
            netutil.connect(conn)
            conn.request(method, path, body=raw, headers=headers)
            del raw
            resp = conn.getresponse()
            netutil.relay(self, resp, "")
        except OSError:
            self._send_json(502, {"error": {"message": "Local edge could not reach the door", "type": "edge_error"}})
        finally:
            conn.close()

    def _static(self, path):
        item = FILES.get(path)
        if item is None:
            self._send_json(404, {"error": {"message": "Not found", "type": "not_found"}})
            return
        name, content_type = item
        target = ROOT / name
        try:
            raw = target.read_bytes()
        except OSError:
            self._send_json(404, {"error": {"message": "Not found", "type": "not_found"}})
            return
        self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.protocol_version = "HTTP/1.1"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def _discard_body(handler):
    length = int(handler.headers.get("Content-Length") or "0")
    if length > 0:
        handler.rfile.read(length)


def _read_json(handler):
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("object")
    return data


def make_server(port, door_port):
    global DOOR_PORT
    DOOR_PORT = door_port
    httpd = ThreadingHTTPServer(("127.0.0.1", port), EdgeHandler)
    httpd.daemon_threads = True
    return httpd
