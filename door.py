"""Door. Admits or rejects, reserves a fixture token budget, and streams the reply.

A request with no free replica waits until the hold budget, then returns 503
and the reserve is released. The local default is long enough for the cold
test. The harness shortens it through the local edge. When ENGINE_URL is set,
readiness is the engine's /health instead of the in-process replica count.
"""

import http.client
import json
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import netutil
import telemetry
import workload

ENGINE_PORT = 8789
ENGINE_HOST = "127.0.0.1"
REMOTE_ENGINE = False
_USAGE = re.compile(r'"usage":(\{[^}]*\})')


class UsageCatcher:
    def __init__(self):
        self.tail = ""
        self.usage = None

    def feed(self, chunk):
        if self.usage is not None:
            return
        self.tail += chunk.decode("utf-8", "replace")
        match = _USAGE.search(self.tail)
        if match:
            try:
                self.usage = json.loads(match.group(1))
            except json.JSONDecodeError:
                self.usage = None
            self.tail = ""
            return
        if len(self.tail) > 800:
            self.tail = self.tail[-240:]


class DoorHandler(BaseHTTPRequestHandler):
    server_version = "door"

    def setup(self):
        super().setup()
        netutil.enable_nodelay(self.connection)

    def log_message(self, fmt, *args):
        print("[door] " + (fmt % args))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"service": "door", "ok": True})
            return
        if path == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{"id": "fixture-model", "object": "model", "owned_by": "fixture"}],
            })
            return
        if path == "/metrics":
            self._send_text(200, telemetry.STATE.door_metrics())
            return
        self._send_json(404, {"error": {"message": "Not found", "type": "not_found"}})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "Not found", "type": "not_found"}})
            return
        trace_id = self.headers.get("x-trace-id") or uuid.uuid4().hex
        if telemetry.STATE.public_trace(trace_id) is None:
            telemetry.STATE.begin_trace(trace_id)
        start = telemetry.STATE.now_ms()
        try:
            self._chat(trace_id)
        finally:
            telemetry.STATE.end_span(trace_id, "door", start, telemetry.STATE.now_ms())

    def _chat(self, trace_id):
        length = int(self.headers.get("Content-Length") or "0")
        if length < 0 or length > 1_000_000:
            self._fail(400, {"error": {"message": "Request body is the wrong size", "type": "bad_request"}})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            prompt_tokens, stream, max_tokens, error, script = _inspect(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._fail(400, {"error": {"message": "Request body must be JSON", "type": "bad_request"}})
            return
        del raw
        if error:
            self._fail(400, {"error": {"message": error, "type": "bad_request"}})
            return
        caller, auth_error = telemetry.STATE.authorize(self.headers.get("Authorization"))
        if auth_error == "missing":
            self._fail(401, {"error": {"message": "Missing fixture credential", "type": "authentication_error"}})
            return
        if auth_error:
            self._fail(401, {"error": {"message": "Unknown fixture credential", "type": "authentication_error"}})
            return
        estimate = prompt_tokens + max_tokens
        if not telemetry.STATE.try_reserve(caller, estimate):
            self._fail(429, {
                "error": {
                    "message": "Fixture token budget exceeded",
                    "type": "insufficient_quota",
                    "code": "rate_limit_exceeded",
                }
            })
            return
        reserved = True
        in_flight = False
        try:
            try:
                if REMOTE_ENGINE:
                    replica, queue_ms = telemetry.STATE.begin_wait_remote(_engine_up)
                else:
                    replica, queue_ms = telemetry.STATE.begin_wait()
            except telemetry.HoldExpired:
                telemetry.STATE.release(caller, estimate)
                reserved = False
                self._fail(503, {"error": {"message": "No replica became ready in time", "type": "unavailable"}})
                return
            except telemetry.Stopping:
                self._fail(503, {"error": {"message": "Door is stopping", "type": "unavailable"}})
                return
            in_flight = True
            sampled = telemetry.STATE.take_sample(trace_id)
            telemetry.STATE.admit(trace_id, replica, queue_ms)
            upstream_body = {
                "model": "fixture-model",
                "stream": stream,
                "max_tokens": max_tokens,
                "prompt_tokens": prompt_tokens,
            }
            if script:
                upstream_body["script"] = script
            upstream = json.dumps(upstream_body).encode("utf-8")
            status, catcher = self._forward(trace_id, replica, sampled, upstream)
            telemetry.STATE.note_status(status)
            if status >= 400:
                telemetry.STATE.release(caller, estimate)
            else:
                actual = _actual_tokens(catcher.usage, estimate)
                telemetry.STATE.settle(caller, estimate, actual)
            reserved = False
        finally:
            if reserved:
                telemetry.STATE.release(caller, estimate)
            if in_flight:
                telemetry.STATE.end_flight()

    def _forward(self, trace_id, replica, sampled, body):
        headers = {
            "content-type": "application/json",
            "content-length": str(len(body)),
            "x-trace-id": trace_id,
            "x-admitted": "1",
            "x-replica": replica,
            "x-sample-tokens": "1" if sampled else "0",
        }
        conn = http.client.HTTPConnection(ENGINE_HOST, ENGINE_PORT, timeout=3600)
        try:
            netutil.connect(conn)
            conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
            resp = conn.getresponse()
            catcher = UsageCatcher()
            netutil.relay(self, resp, trace_id, catcher.feed)
            return resp.status, catcher
        finally:
            conn.close()

    def _fail(self, status, payload):
        telemetry.STATE.note_status(status)
        self._send_json(status, payload)

    def _send_text(self, status, body):
        raw = body.encode("utf-8")
        self.protocol_version = "HTTP/1.1"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
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


def _inspect(raw):
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        return 0, False, None, "Request body must be a JSON object", ""
    prompt_tokens = telemetry.count_messages(data)
    stream = bool(data.get("stream"))
    max_tokens, error = telemetry.parse_max_tokens(data)
    script = "" if error else workload.script_for(data)
    return prompt_tokens, stream, max_tokens, error, script


def _actual_tokens(usage, estimate):
    if not isinstance(usage, dict):
        return estimate
    try:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return estimate
    return max(0, prompt + completion)


def _engine_up():
    conn = http.client.HTTPConnection(ENGINE_HOST, ENGINE_PORT, timeout=1)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        return resp.status == 200
    except (OSError, TimeoutError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def make_server(port, engine_port, host="127.0.0.1"):
    global ENGINE_PORT
    ENGINE_PORT = engine_port
    httpd = ThreadingHTTPServer((host, port), DoorHandler)
    httpd.daemon_threads = True
    return httpd
