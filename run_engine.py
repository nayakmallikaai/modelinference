"""Process entry for the fake engine image. One process is one replica."""

import os

import fake_engine


def main():
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("BIND", "0.0.0.0")
    httpd = fake_engine.make_server(port, host)
    print("fake engine listening on %s:%d" % (host, port), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
