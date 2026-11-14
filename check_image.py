"""Phase 6 gate. Build the door image and read the two workflows.

The pinned vLLM image is not pulled. gpu-window.yml is not executed.
"""

import subprocess
from pathlib import Path

import check_manifests

ROOT = Path(__file__).resolve().parent
PHRASE = "phase six fixed string"
PIN = "vllm/vllm-openai:v0.10.2"

FAILURES = []


def say(text):
    print(text, flush=True)


def fail(message):
    FAILURES.append(message)
    say("Check failed: " + message)


def load_workflow(path):
    docs = [
        doc
        for doc in check_manifests.load_yaml_documents(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]
    if len(docs) != 1:
        fail("%s is not one workflow document" % path.name)
        return None
    return docs[0]


def trigger_names(doc):
    raw = None
    for key in ("on", True, "true"):
        if key in doc:
            raw = doc.get(key)
            break
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {str(item) for item in raw}
    if isinstance(raw, dict):
        return {str(key) for key in raw}
    return set()


def runs_on_values(doc):
    found = []
    for job in (doc.get("jobs") or {}).values():
        if isinstance(job, dict):
            found.append(job.get("runs-on"))
    return found


def check_local():
    path = ROOT / ".github" / "workflows" / "local.yml"
    text = path.read_text(encoding="utf-8")
    doc = load_workflow(path)
    if doc is None:
        return
    triggers = trigger_names(doc)
    lowered = text.lower()
    gpu_runner = any(isinstance(value, str) and "gpu" in value.lower() for value in runs_on_values(doc))
    if (
        "pull_request" not in triggers
        or "workflow_dispatch" in triggers
        or gpu_runner
        or "nvidia" in lowered
        or "g6" in lowered
        or "kind" in lowered
        or "python3 harness.py" not in text
        or "python3 check_manifests.py" not in text
    ):
        fail("local.yml must run on pull_request, run the harness and the manifest check, and stay off GPU and kind")
        return
    say("local.yml triggers on pull_request and has no GPU job.")


def job_timeouts(doc):
    values = []
    for job in (doc.get("jobs") or {}).values():
        if isinstance(job, dict):
            values.append(job.get("timeout-minutes"))
    return values


def has_destroy(doc):
    for job in (doc.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            clause = str(step.get("if", ""))
            blob = (str(step.get("name", "")) + " " + str(step.get("run", ""))).lower()
            if clause == "always()" and "destroy" in blob:
                return True
    return False


def has_environment(doc):
    for job in (doc.get("jobs") or {}).values():
        if isinstance(job, dict) and job.get("environment"):
            return True
    return False


def check_gpu_workflow():
    path = ROOT / ".github" / "workflows" / "gpu-window.yml"
    doc = load_workflow(path)
    if doc is None:
        return
    group = doc.get("concurrency")
    if isinstance(group, dict):
        group = group.get("group")
    timeouts = job_timeouts(doc)
    if (
        trigger_names(doc) != {"workflow_dispatch"}
        or not timeouts
        or any(not isinstance(value, int) or value > 45 or value < 1 for value in timeouts)
        or not isinstance(group, str)
        or not group
        or not has_destroy(doc)
        or not has_environment(doc)
    ):
        fail("gpu-window.yml must be dispatch-only, at most 45 minutes, with concurrency and an always destroy step")
        return
    script = (ROOT / "deploy" / "scripts" / "window.py").read_text(encoding="utf-8")
    if 'os.environ.get("GPU_WINDOW_CONFIRM") != "yes"' not in script:
        fail("the window script must refuse to start unless GPU_WINDOW_CONFIRM=yes")
        return
    say("gpu-window.yml is dispatch-only, times out within 45 minutes, and always destroys.")
    say("gpu-window.yml was not executed.")


def check_pin():
    text = (ROOT / "deploy" / "vllm" / "deployment.yaml").read_text(encoding="utf-8")
    door = (ROOT / "Dockerfile.door").read_text(encoding="utf-8")
    engine = (ROOT / "Dockerfile.engine").read_text(encoding="utf-8")
    if PIN not in text or PIN in door or PIN in engine or "FROM vllm" in door or "FROM vllm" in engine:
        fail("the vLLM tag is not pinned, or a local image pulls it")
        return
    say("The pinned vLLM tag is %s. This phase did not pull it." % PIN)


def run_container(args, timeout=120):
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python3", "inference-door:local", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result


def check_count():
    counts = []
    for _ in range(2):
        result = run_container(["token_count.py", PHRASE])
        if result.returncode != 0:
            fail("token count exited %s (%s)" % (result.returncode, (result.stderr or "").strip()))
            return
        text = result.stdout.strip()
        say(text)
        if not text.isdigit():
            fail("token count was not an integer")
            return
        counts.append(int(text))
    if counts[0] != counts[1] or counts[0] < 1:
        fail("the two token counts differ")
        return
    say("A one-shot container printed %d tokens twice." % counts[0])


def check_files():
    script = (
        "import os\n"
        "bad = []\n"
        "for root, dirs, files in os.walk('/'):\n"
        "    if root == '/':\n"
        "        dirs[:] = [name for name in dirs if name not in {'proc', 'sys', 'dev'}]\n"
        "    for name in files:\n"
        "        if name.endswith('.safetensors'):\n"
        "            bad.append(os.path.join(root, name))\n"
        "need = ['/app/tokenizer/tokenizer.json', '/app/tokenizer/tokenizer_config.json']\n"
        "missing = [path for path in need if not os.path.isfile(path)]\n"
        "print('missing=' + ','.join(missing) + ' safetensors=' + ','.join(bad))\n"
    )
    result = run_container(["-c", script])
    if result.returncode != 0:
        fail("image file scan failed (%s)" % (result.stderr or "").strip())
        return
    if result.stdout.strip() != "missing= safetensors=":
        fail("the image is missing tokenizer files or contains safetensors (%s)" % result.stdout.strip())
        return
    say("The door image has the tokenizer files and no safetensors.")


def main():
    check_local()
    check_gpu_workflow()
    check_pin()
    if FAILURES:
        return 1
    build = subprocess.run(
        ["docker", "build", "-f", "Dockerfile.door", "-t", "inference-door:local", "."],
        cwd=ROOT,
    )
    if build.returncode != 0:
        fail("the door image did not build")
        return 1
    say("The door image built.")
    check_count()
    check_files()
    if FAILURES:
        return 1
    say("Phase 6 passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
