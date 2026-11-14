"""Fake engine. Chat completions, streaming and buffered. No prompt text is kept."""

import json
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import telemetry
import workload

PIECES = ("Fixture", "reply", "from", "the", "local", "engine")
STREAM_GAP = 0.03
BUFFER_GAP = 0.004
PREFILL = 0.02


def _json_bytes(payload):
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _read_json(handler):
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _completion_id(trace_id):
    tail = (trace_id or "local")[:12]
    return "fixture-" + tail


class EngineHandler(BaseHTTPRequestHandler):
    server_version = "fake-engine"

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, fmt, *args):
        print("[fake engine] " + (fmt % args))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"service": "fake engine", "ok": True})
            return
        if path == "/ready":
            ready = telemetry.STATE.snapshot()["readyReplicas"]
            if ready > 0:
                self._send_json(200, {"service": "fake engine", "ready": True, "readyReplicas": ready})
            else:
                self._send_json(503, {"service": "fake engine", "ready": False, "readyReplicas": 0})
            return
        if path == "/v1/models":
            self._send_json(200, _model_list())
            return
        if path == "/metrics":
            self._send_text(200, telemetry.STATE.engine_metrics())
            return
        self._send_json(404, {"error": {"message": "Not found", "type": "not_found"}})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "Not found", "type": "not_found"}})
            return
        trace_id = self.headers.get("x-trace-id") or ""
        start = telemetry.STATE.now_ms()
        try:
            self._complete(trace_id)
        finally:
            if trace_id:
                telemetry.STATE.end_span(trace_id, "fake engine", start, telemetry.STATE.now_ms())

    def _complete(self, trace_id):
        admitted = self.headers.get("x-admitted") == "1"
        if not admitted and telemetry.STATE.snapshot()["readyReplicas"] <= 0:
            self._send_json(503, {"error": {"message": "Fake engine is unready", "type": "engine_unready"}})
            return
        try:
            payload = _read_json(self)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send_json(400, {"error": {"message": "Request body must be JSON", "type": "bad_request"}})
            return
        max_tokens, error = telemetry.parse_max_tokens(payload)
        if error:
            self._send_json(400, {"error": {"message": error, "type": "bad_request"}})
            return
        if "prompt_tokens" in payload and "messages" not in payload:
            try:
                prompt_tokens = int(payload.get("prompt_tokens") or 0)
            except (TypeError, ValueError):
                prompt_tokens = 0
        else:
            prompt_tokens = telemetry.count_messages(payload)
            payload.pop("messages", None)
        stream = bool(payload.get("stream"))
        script = payload.get("script") if isinstance(payload.get("script"), str) else ""
        if script not in workload.PLANS:
            script = ""
        sampled = self.headers.get("x-sample-tokens") == "1"
        replica = self.headers.get("x-replica") or "replica-1"
        telemetry.STATE.engine_enter()
        try:
            if stream:
                self._stream_live(trace_id, replica, prompt_tokens, max_tokens, sampled, script)
                return
            started = time.perf_counter()
            pieces, times = _produce(max_tokens, False, trace_id, sampled, started, script)
            ttft, itl = _timing(started, times)
            e2e = round((times[-1] - started) * 1000, 1) if times else 0
            if trace_id:
                telemetry.STATE.finish(trace_id, replica, prompt_tokens, len(pieces), ttft, itl, e2e)
            self._send_json(200, _buffered_body(trace_id, pieces, prompt_tokens), trace_id)
        finally:
            telemetry.STATE.engine_leave()

    def _stream_live(self, trace_id, replica, prompt_tokens, max_tokens, sampled, script):
        completion = _completion_id(trace_id)
        self.close_connection = True
        self.protocol_version = "HTTP/1.0"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if trace_id:
            self.send_header("X-Trace-Id", trace_id)
        self.end_headers()
        if not self._push(_sse(completion, {"role": "assistant"}, None, None)):
            return
        started = time.perf_counter()
        pieces, gap = _pieces_for(script, max_tokens, True)
        times = []
        for index, piece in enumerate(pieces):
            time.sleep(PREFILL if index == 0 else gap)
            now = time.perf_counter()
            times.append(now)
            if trace_id and index == 0:
                telemetry.STATE.first_token(trace_id, round((now - started) * 1000, 1))
            if trace_id and sampled:
                telemetry.STATE.token(trace_id, index)
            prefix = "" if index == 0 else " "
            if not self._push(_sse(completion, {"content": prefix + piece}, None, None)):
                return
        ttft, itl = _timing(started, times)
        e2e = round((times[-1] - started) * 1000, 1) if times else 0
        if trace_id:
            telemetry.STATE.finish(trace_id, replica, prompt_tokens, len(pieces), ttft, itl, e2e)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(pieces),
            "total_tokens": prompt_tokens + len(pieces),
        }
        if not self._push(_sse(completion, {}, "stop", usage)):
            return
        self._push(b"data: [DONE]\n\n")

    def _push(self, payload):
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
            return False
        return True

    def _send_text(self, status, body):
        raw = body.encode("utf-8")
        self.protocol_version = "HTTP/1.1"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, status, payload, trace_id=""):
        raw = _json_bytes(payload)
        self.protocol_version = "HTTP/1.1"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if trace_id:
            self.send_header("X-Trace-Id", trace_id)
        self.end_headers()
        self.wfile.write(raw)


def _pieces_for(script, max_tokens, stream):
    planned = workload.plan_pieces(script, max_tokens) if script else None
    if planned is None:
        count = min(len(PIECES), max_tokens)
        gap = STREAM_GAP if stream else BUFFER_GAP
        return list(PIECES[:count]), gap
    return planned


def _produce(max_tokens, stream, trace_id, sampled, started, script):
    pieces, gap = _pieces_for(script, max_tokens, stream)
    times = []
    for index, _piece in enumerate(pieces):
        time.sleep(PREFILL if index == 0 else gap)
        now = time.perf_counter()
        times.append(now)
        if trace_id and index == 0:
            telemetry.STATE.first_token(trace_id, round((now - started) * 1000, 1))
        if trace_id and sampled:
            telemetry.STATE.token(trace_id, index)
    return pieces, times


def _timing(started, times):
    if not times:
        return 0, None
    ttft = round((times[0] - started) * 1000, 1)
    if len(times) < 2:
        return ttft, None
    gaps = [(times[index] - times[index - 1]) * 1000 for index in range(1, len(times))]
    return ttft, round(sum(gaps) / len(gaps), 1)


def _buffered_body(trace_id, pieces, prompt_tokens):
    return {
        "id": _completion_id(trace_id),
        "object": "chat.completion",
        "model": "fixture-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": " ".join(pieces)},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(pieces),
            "total_tokens": prompt_tokens + len(pieces),
        },
    }


def _sse(completion, delta, finish, usage):
    payload = {
        "id": completion,
        "object": "chat.completion.chunk",
        "model": "fixture-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
    return b"data: " + _json_bytes(payload) + b"\n\n"


def _model_list():
    return {
        "object": "list",
        "data": [{"id": "fixture-model", "object": "model", "owned_by": "fixture"}],
    }


def make_server(port, host="127.0.0.1"):
    httpd = ThreadingHTTPServer((host, port), EngineHandler)
    httpd.daemon_threads = True
    return httpd
