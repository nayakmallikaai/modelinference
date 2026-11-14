"""Process entry for the door image. Binds the container port and the engine URL."""

import os
from urllib.parse import urlparse

import door


def _configure_engine():
    url = os.environ.get("ENGINE_URL", "").strip()
    if not url:
        door.REMOTE_ENGINE = False
        door.ENGINE_HOST = "127.0.0.1"
        return int(os.environ.get("ENGINE_PORT", "8789"))
    parsed = urlparse(url)
    if parsed.scheme not in {"http", ""} or not parsed.hostname:
        raise SystemExit("ENGINE_URL must be an http URL")
    door.REMOTE_ENGINE = True
    door.ENGINE_HOST = parsed.hostname
    return parsed.port or 80


def main():
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("BIND", "0.0.0.0")
    engine_port = _configure_engine()
    httpd = door.make_server(port, engine_port, host)
    print("door listening on %s:%d" % (host, port), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
