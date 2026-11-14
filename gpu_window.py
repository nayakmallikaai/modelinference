"""Entrypoint for the GPU window. Pull requests do not run this file.

`run` creates the cluster only when GPU_WINDOW_CONFIRM=yes.
`destroy` deletes the cluster that run recorded.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "deploy" / "scripts"))

import window


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "run":
        return window.run()
    if command == "destroy":
        return window.destroy()
    print("Use run or destroy.", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
