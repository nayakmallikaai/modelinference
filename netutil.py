"""Small HTTP helpers. They copy bytes and do not keep request text."""

import socket


def enable_nodelay(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


def connect(conn):
    conn.connect()
    enable_nodelay(conn.sock)
    return conn


def iter_body(resp):
    remaining = resp.length
    if remaining == 0:
        return
    if remaining is None:
        while True:
            chunk = resp.fp.read1(4096)
            if not chunk:
                break
            yield chunk
        return
    while remaining > 0:
        chunk = resp.fp.read1(min(4096, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        yield chunk


def relay(handler, resp, trace_id="", on_bytes=None):
    try:
        if resp.length is None:
            handler.close_connection = True
            handler.protocol_version = "HTTP/1.0"
        else:
            handler.protocol_version = "HTTP/1.1"
        handler.send_response(resp.status)
        content_type = resp.getheader("Content-Type")
        if content_type:
            handler.send_header("Content-Type", content_type)
        if resp.length is not None:
            handler.send_header("Content-Length", str(resp.length))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        if trace_id:
            handler.send_header("X-Trace-Id", trace_id)
        handler.end_headers()
        for chunk in iter_body(resp):
            if on_bytes is not None:
                on_bytes(chunk)
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True
