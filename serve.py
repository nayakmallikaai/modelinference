"""Start the local edge, the door, and the fake engine.

From this folder:

    python3 serve.py

Then open http://127.0.0.1:8787/

Fixture credentials are test doubles, not an auth choice. fixture-alpha's budget
is 8 tokens, so a request that reserves more than that gets 429. fixture-beta
still has room. A missing credential does not move token counters. Ready replicas
at 0 leave the door up and show People waiting.

A hold past the budget returns 503 and releases the reserve. Metrics are on
the door and the fake engine at /metrics. The serving page is /serving.

The harness prints a warm report and a separate cold report:

    python3 harness.py
"""

import threading

import fake_engine
import door
import local_edge
import telemetry

EDGE_PORT = 8787
DOOR_PORT = 8788
ENGINE_PORT = 8789


def _start(httpd, name):
    thread = threading.Thread(target=httpd.serve_forever, name=name, daemon=True)
    thread.start()
    return thread


def main():
    engine = fake_engine.make_server(ENGINE_PORT)
    door_server = door.make_server(DOOR_PORT, ENGINE_PORT)
    edge = local_edge.make_server(EDGE_PORT, DOOR_PORT)
    _start(engine, "fake-engine")
    _start(door_server, "door")
    _start(edge, "local-edge")
    print("local edge   http://127.0.0.1:%d/" % EDGE_PORT, flush=True)
    print("door         http://127.0.0.1:%d/health" % DOOR_PORT, flush=True)
    print("fake engine  http://127.0.0.1:%d/ready" % ENGINE_PORT, flush=True)
    print("fixture credentials (test doubles, not an auth decision):", flush=True)
    print("  Authorization: Bearer fixture-beta    budget 100000 tokens", flush=True)
    print("  Authorization: Bearer fixture-alpha   budget 8 tokens", flush=True)
    print("A missing credential does not move token counters.", flush=True)
    print("Ready replicas 0: the request waits, People waiting changes, the door stays up.", flush=True)
    print("Harness: python3 harness.py", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        telemetry.STATE.close()
        edge.shutdown()
        door_server.shutdown()
        engine.shutdown()


if __name__ == "__main__":
    main()
