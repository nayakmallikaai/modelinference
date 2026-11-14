"""Phase 5 gate. Scale the fake engine 0 to 1 to 0 on a local kind cluster.

Docker is required. The script builds the CPU images, installs Prometheus and
KEDA, and applies the window ScaledObject patch. It does not apply Karpenter
pools and it does not call AWS. The kind cluster is deleted before exit.
"""

import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / ".tools"
KUBECONFIG = TOOLS / "kubeconfig"
CLUSTER = "inference-kind"
CONTEXT = "kind-" + CLUSTER
DOOR_LOCAL = 18080
PROM_LOCAL = 19090
NODE_IMAGE = "kindest/node:v1.32.2"
KIND_VERSIONS = ("v0.29.0", "v0.27.0", "v0.26.0")
KEDA_VERSIONS = ("2.17.2", "2.17.1", "2.17.0", "2.16.1")
BUSY = (
    "door_requests_waiting",
    "door_requests_in_flight",
    "vllm:num_requests_waiting",
    "vllm:num_requests_running",
)

FAILURES = []
ENV = os.environ.copy()
FORWARDS = []


class CommandFailed(RuntimeError):
    pass


def say(text):
    print(text, flush=True)


def fail(message):
    FAILURES.append(message)
    say("Check failed: " + message)


def tool_env():
    env = os.environ.copy()
    env["KUBECONFIG"] = str(KUBECONFIG)
    for key in list(env):
        if key.startswith("AWS_"):
            del env[key]
    return env


def run(args, timeout=120, capture=False):
    say("+ " + " ".join(args))
    if capture:
        result = subprocess.run(
            args,
            env=ENV,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise CommandFailed(detail or " ".join(args))
        return result.stdout
    result = subprocess.run(args, env=ENV, timeout=timeout)
    if result.returncode != 0:
        raise CommandFailed(" ".join(args))
    return ""


def docker_ready():
    deadline = time.time() + 90
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True
        time.sleep(3)
    return False


def build_images():
    run(["docker", "build", "-f", "Dockerfile.door", "-t", "inference-door:local", "."], timeout=600)
    run(["docker", "build", "-f", "Dockerfile.engine", "-t", "inference-engine:local", "."], timeout=600)


def kind_bin():
    found = shutil.which("kind")
    if found:
        return found
    local = TOOLS / "kind"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    system = "darwin" if sys.platform == "darwin" else "linux"
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "amd64"
    TOOLS.mkdir(parents=True, exist_ok=True)
    last = "kind download failed"
    for version in KIND_VERSIONS:
        url = "https://kind.sigs.k8s.io/dl/%s/kind-%s-%s" % (version, system, arch)
        try:
            urllib.request.urlretrieve(url, local)
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
            continue
        local.chmod(local.stat().st_mode | stat.S_IEXEC)
        return str(local)
    raise CommandFailed(last)


def cluster_exists(binary):
    out = run([binary, "get", "clusters"], capture=True)
    return CLUSTER in out.split()


def create_cluster(binary):
    TOOLS.mkdir(parents=True, exist_ok=True)
    if KUBECONFIG.exists():
        KUBECONFIG.unlink()
    run(
        [
            binary,
            "create",
            "cluster",
            "--name",
            CLUSTER,
            "--image",
            NODE_IMAGE,
            "--kubeconfig",
            str(KUBECONFIG),
        ],
        timeout=600,
    )


def delete_cluster(binary):
    say("Deleting the kind cluster.")
    result = subprocess.run(
        [binary, "delete", "cluster", "--name", CLUSTER],
        env=ENV,
        timeout=180,
    )
    if result.returncode != 0:
        fail("the kind cluster was not deleted")


def install_keda():
    destination = TOOLS / "keda.yaml"
    last = "KEDA download failed"
    for version in KEDA_VERSIONS:
        url = "https://github.com/kedacore/keda/releases/download/v%s/keda-%s.yaml" % (version, version)
        try:
            urllib.request.urlretrieve(url, destination)
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
            continue
        if destination.stat().st_size < 1000:
            last = "KEDA manifest for %s was empty" % version
            continue
        run(["kubectl", "apply", "--server-side", "-f", str(destination)], timeout=180)
        return
    raise CommandFailed(last)


def wait_rollout(namespace, name, timeout):
    run(
        ["kubectl", "rollout", "status", "deployment/%s" % name, "-n", namespace, "--timeout=%ds" % timeout],
        timeout=timeout + 20,
    )


def json_patch(patch):
    run(
        [
            "kubectl",
            "patch",
            "scaledobject",
            "vllm",
            "-n",
            "inference",
            "--type",
            "json",
            "--patch",
            json.dumps(patch),
        ]
    )


def apply_workloads():
    run(["kubectl", "apply", "-f", "deploy/namespace.yaml"])
    run(["kubectl", "apply", "-f", "kind/prometheus.yaml"])
    run(["kubectl", "apply", "-f", "deploy/door/deployment.yaml"])
    run(["kubectl", "apply", "-f", "deploy/door/service.yaml"])
    run(["kubectl", "apply", "-f", "kind/engine.yaml"])
    run(["kubectl", "apply", "-f", "deploy/vllm/service.yaml"])
    wait_rollout("monitoring", "prometheus", 180)
    apply_file("deploy/keda/scaledobject.yaml")
    run(
        [
            "kubectl",
            "patch",
            "scaledobject",
            "vllm",
            "-n",
            "inference",
            "--type",
            "merge",
            "--patch-file",
            "deploy/overlays/window/scaledobject-patch.yaml",
        ]
    )
    json_patch(
        [
            {
                "op": "add",
                "path": "/spec/triggers/0/metadata/activationThreshold",
                "value": "0",
            },
            {
                "op": "add",
                "path": "/spec/advanced",
                "value": {
                    "horizontalPodAutoscalerConfig": {
                        "behavior": {
                            "scaleDown": {
                                "stabilizationWindowSeconds": 0,
                                "policies": [{"type": "Pods", "value": 1, "periodSeconds": 15}],
                            }
                        }
                    }
                },
            },
        ]
    )
    run(["kubectl", "set", "env", "deployment/door", "-n", "inference", "HOLD_BUDGET_SECONDS=240"])
    wait_rollout("inference", "door", 180)


def apply_file(path):
    last = "apply failed"
    for _ in range(12):
        say("+ kubectl apply -f " + path)
        result = subprocess.run(
            ["kubectl", "apply", "-f", path],
            env=ENV,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            if result.stdout.strip():
                say(result.stdout.strip())
            return
        last = (result.stderr or result.stdout or "apply failed").strip()
        time.sleep(5)
    raise CommandFailed(last)


def start_forward(local_port, namespace, service, remote_port):
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", namespace, "svc/%s" % service, "%d:%d" % (local_port, remote_port)],
        env=ENV,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    FORWARDS.append(proc)
    return proc


def stop_forwards():
    for proc in FORWARDS:
        if proc.poll() is None:
            proc.terminate()
    for proc in FORWARDS:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    FORWARDS.clear()


def wait_port(port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = socket.create_connection(("127.0.0.1", port), 1)
        except OSError:
            time.sleep(0.3)
            continue
        conn.close()
        return True
    return False


def replica_counts(name):
    out = run(["kubectl", "get", "deploy", name, "-n", "inference", "-o", "json"], capture=True)
    doc = json.loads(out)
    spec = int((doc.get("spec") or {}).get("replicas") or 0)
    ready = int((doc.get("status") or {}).get("readyReplicas") or 0)
    return spec, ready


def prom_query(expr):
    url = "http://127.0.0.1:%d/api/v1/query?%s" % (
        PROM_LOCAL,
        urllib.parse.urlencode({"query": expr}),
    )
    with urllib.request.urlopen(url, timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = ((payload.get("data") or {}).get("result")) or []
    if not result:
        return None
    return float(result[0]["value"][1])


def chat(token, content, max_tokens, timeout):
    body = json.dumps(
        {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % DOOR_LOCAL,
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def assert_context():
    context = run(["kubectl", "config", "current-context"], capture=True).strip()
    if context != CONTEXT:
        fail("kubeconfig context is %s" % context)
        return False
    return True


def assert_no_karpenter():
    result = subprocess.run(
        ["kubectl", "get", "nodepools.karpenter.sh,ec2nodeclasses.karpenter.k8s.aws", "-A"],
        env=ENV,
        capture_output=True,
        text=True,
    )
    combined = (result.stdout or "") + (result.stderr or "")
    missing = "doesn't have a resource type" in combined or "NotFound" in combined
    if missing:
        say("Karpenter pools were not created.")
        return True
    rows = [
        line
        for line in (result.stdout or "").splitlines()
        if line and not line.startswith("NAME") and not line.startswith("NAMESPACE")
    ]
    if result.returncode != 0 or rows:
        fail("Karpenter pools are present")
        return False
    say("Karpenter pools were not created.")
    return True


def assert_window_scaler():
    out = run(["kubectl", "get", "scaledobject", "vllm", "-n", "inference", "-o", "json"], capture=True)
    spec = json.loads(out).get("spec") or {}
    query = ""
    for trigger in spec.get("triggers") or []:
        query += ((trigger.get("metadata") or {}).get("query")) or ""
    missing = [name for name in BUSY if name not in query]
    if spec.get("minReplicaCount") != 0 or spec.get("maxReplicaCount") != 1 or spec.get("cooldownPeriod") != 60 or missing:
        fail("the window ScaledObject was not applied")
        return False
    ingress = run(["kubectl", "get", "ingress", "-n", "inference", "-o", "name"], capture=True).strip()
    if ingress:
        fail("an Ingress was applied on kind")
        return False
    return True


def wait_idle():
    wait_rollout("keda", "keda-operator", 180)
    wait_rollout("inference", "door", 180)
    deadline = time.time() + 120
    while time.time() < deadline:
        out = run(["kubectl", "get", "scaledobject", "vllm", "-n", "inference", "-o", "json"], capture=True)
        conditions = (json.loads(out).get("status") or {}).get("conditions") or []
        ready = any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions)
        spec, replicas = replica_counts("vllm")
        door_spec, door_ready = replica_counts("door")
        if ready and spec == 0 and replicas == 0 and door_ready >= 1 and door_spec >= 1:
            try:
                value = prom_query("sum(door_requests_waiting or vector(0))")
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, ValueError):
                value = None
            if value == 0:
                say("Before any request, the door is ready and the engine Deployment has 0 ready replicas.")
                return True
        time.sleep(3)
    fail("the door was not ready at 0 engine replicas")
    return False


def check_scale_up():
    holder = {}
    samples = []
    inflight = {"on": True}

    def call():
        try:
            holder["result"] = chat("fixture-beta", "hello kind", 4, 240)
        except Exception as exc:
            holder["error"] = exc
        finally:
            inflight["on"] = False

    def watch():
        while inflight["on"]:
            try:
                samples.append(replica_counts("vllm"))
            except (CommandFailed, json.JSONDecodeError, subprocess.TimeoutExpired):
                pass
            time.sleep(0.5)

    caller = threading.Thread(target=call)
    watcher = threading.Thread(target=watch)
    caller.start()
    watcher.start()
    caller.join(timeout=250)
    watcher.join(timeout=5)
    if caller.is_alive():
        fail("the authorized request did not finish")
        return False
    if "error" in holder:
        fail("the authorized request failed (%s)" % holder["error"])
        return False
    status, _body = holder["result"]
    saw_one = any(spec >= 1 for spec, _ready in samples)
    if status != 200 or not saw_one:
        fail("authorized status %s, replica samples %s" % (status, samples[-6:]))
        return False
    say("One authorized request returned 200, and the engine Deployment was at 1 replica before the response finished.")
    return True


def check_scale_down():
    deadline = time.time() + 150
    while time.time() < deadline:
        spec, ready = replica_counts("vllm")
        if spec == 0 and ready == 0:
            say("After the short cooldown, ready replicas returned to 0.")
            return True
        time.sleep(3)
    fail("ready replicas did not return to 0 after the cooldown")
    return False


def check_rejection():
    status, _body = chat("fixture-alpha", "over budget", 32, 30)
    if status != 429:
        fail("expected 429 while replicas were 0, got %s" % status)
        return False
    deadline = time.time() + 20
    while time.time() < deadline:
        spec, ready = replica_counts("vllm")
        if spec != 0 or ready != 0:
            fail("a 429 moved the engine to %d replicas" % spec)
            return False
        time.sleep(2)
    say("A 429 while replicas were 0 left replicas at 0 for the following 20 seconds.")
    return True


def debug_dump():
    commands = [
        ["kubectl", "get", "pods", "-A"],
        ["kubectl", "get", "deploy", "-n", "inference"],
        ["kubectl", "describe", "scaledobject", "vllm", "-n", "inference"],
        ["kubectl", "logs", "-n", "inference", "deploy/door", "--tail=40"],
        ["kubectl", "logs", "-n", "keda", "deploy/keda-operator", "--tail=40"],
    ]
    for args in commands:
        say("+ " + " ".join(args))
        result = subprocess.run(args, env=ENV, capture_output=True, text=True, timeout=30)
        text = (result.stdout or "") + (result.stderr or "")
        if text.strip():
            say(text[-2000:])
    try:
        value = prom_query(
            "sum(door_requests_waiting or vector(0)) + sum(door_requests_in_flight or vector(0))"
        )
        say("prometheus busy door sum: %s" % value)
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        say("prometheus query failed: %s" % exc)


def main():
    global ENV
    if not docker_ready():
        fail("Docker is not running")
        return 1
    if shutil.which("kubectl") is None:
        fail("kubectl is not installed")
        return 1
    ENV = tool_env()
    binary = None
    created = False
    try:
        build_images()
        binary = kind_bin()
        if cluster_exists(binary):
            fail("kind cluster %s already exists" % CLUSTER)
            return 1
        created = True
        create_cluster(binary)
        if not assert_context():
            return 1
        run(
            [binary, "load", "docker-image", "--name", CLUSTER, "inference-door:local", "inference-engine:local"],
            timeout=180,
        )
        install_keda()
        wait_rollout("keda", "keda-operator", 180)
        apply_workloads()
        start_forward(PROM_LOCAL, "monitoring", "prometheus", 9090)
        start_forward(DOOR_LOCAL, "inference", "door", 8080)
        if not wait_port(PROM_LOCAL, 40) or not wait_port(DOOR_LOCAL, 40):
            fail("port-forward did not open")
            return 1
        if not assert_no_karpenter() or not assert_window_scaler() or not wait_idle():
            return 1
        if not check_scale_up() or not check_scale_down() or not check_rejection():
            return 1
        say("The kubeconfig is the kind cluster %s. No AWS API was called." % CONTEXT)
    except (CommandFailed, subprocess.TimeoutExpired, OSError) as exc:
        fail(str(exc))
    finally:
        if FAILURES:
            try:
                debug_dump()
            except (CommandFailed, subprocess.TimeoutExpired, OSError) as exc:
                say("debug dump failed: %s" % exc)
        stop_forwards()
        if created and binary:
            try:
                delete_cluster(binary)
            except (CommandFailed, subprocess.TimeoutExpired, OSError) as exc:
                fail(str(exc))
    if FAILURES:
        return 1
    say("Phase 5 passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
