"""Phase 4 gate. Reads deploy/ and exits 0 when the manifests match the design.

No cluster is created and no image is pulled. kubeconform checks the API
documents. The rest of this file checks the fields the design names.
"""

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEPLOY = ROOT / "deploy"
OVERLAY = DEPLOY / "overlays" / "window"
BUSY = (
    "door_requests_waiting",
    "door_requests_in_flight",
    "vllm:num_requests_waiting",
    "vllm:num_requests_running",
)
SCHEMA = "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"

FAILURES = []


def say(text):
    print(text, flush=True)


def fail(message):
    FAILURES.append(message)
    say("Check failed: " + message)


def load_yaml_documents(text):
    try:
        import yaml
    except ModuleNotFoundError:
        yaml = None
    if yaml is not None:
        return [doc for doc in yaml.safe_load_all(text)]
    script = (
        "require 'yaml'\n"
        "require 'json'\n"
        "YAML.load_stream(STDIN.read).each do |doc|\n"
        "  next if doc.nil?\n"
        "  puts JSON.generate(doc)\n"
        "end\n"
    )
    result = subprocess.run(
        ["ruby", "-e", script],
        input=text,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ruby could not read YAML")
    docs = []
    for line in result.stdout.splitlines():
        if line.strip():
            docs.append(json.loads(line))
    return docs


def load_documents():
    found = []
    for path in sorted(DEPLOY.rglob("*")):
        if path.suffix not in {".yaml", ".yml"}:
            continue
        text = path.read_text(encoding="utf-8")
        try:
            docs = load_yaml_documents(text)
        except (RuntimeError, json.JSONDecodeError) as exc:
            fail("%s is not valid YAML (%s)" % (path.relative_to(ROOT), exc))
            continue
        for doc in docs:
            if isinstance(doc, dict) and doc.get("kind"):
                found.append((path, doc))
    return found


def overlay(path):
    try:
        path.relative_to(OVERLAY)
    except ValueError:
        return False
    return True


def kind_named(docs, kind, name, base_only=True):
    matches = []
    for path, doc in docs:
        if base_only and overlay(path):
            continue
        if doc.get("kind") == kind and (doc.get("metadata") or {}).get("name") == name:
            matches.append((path, doc))
    return matches


def pod_spec(doc):
    return ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}


def containers(doc):
    spec = pod_spec(doc)
    found = list(spec.get("containers") or [])
    found.extend(spec.get("initContainers") or [])
    return found


def gpu_quantity(value):
    if value is None:
        return None
    text = str(value).strip()
    if text in {"1", "1.0"}:
        return 1
    return None


def resource_gpu(container):
    resources = container.get("resources") or {}
    for section in ("requests", "limits"):
        block = resources.get(section) or {}
        if "nvidia.com/gpu" in block:
            return gpu_quantity(block.get("nvidia.com/gpu"))
    return None


def has_gpu_toleration(spec):
    for item in spec.get("tolerations") or []:
        if isinstance(item, dict) and item.get("key") == "nvidia.com/gpu":
            return True
    return False


def kubeconform_bin():
    found = shutil.which("kubeconform")
    if found:
        return found
    local = ROOT / ".tools" / "kubeconform"
    if local.is_file():
        return str(local)
    return None


def run_kubeconform(docs):
    binary = kubeconform_bin()
    if binary is None:
        fail("kubeconform is not installed")
        return
    paths = sorted({
        str(path) for path, doc in docs
        if not overlay(path) and doc.get("kind") != "Kustomization"
    })
    command = [
        binary,
        "-strict",
        "-summary",
        "-cache",
        str(ROOT / ".tools" / "kubeconform-cache"),
        "-kubernetes-version",
        "1.32.0",
        "-skip",
        "Kustomization",
        "-schema-location",
        "default",
        "-schema-location",
        SCHEMA,
    ]
    command.extend(paths)
    result = subprocess.run(command, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        detail = " ".join(line.strip() for line in output.strip().splitlines() if line.strip())
        if len(detail) > 500:
            detail = detail[:500]
        fail("kubeconform rejected a document (" + (detail or "kubeconform failed") + ")")
        return
    say("kubeconform accepted the API documents, including KEDA and Karpenter.")


def check_vllm(docs):
    matches = kind_named(docs, "Deployment", "vllm")
    if len(matches) != 1:
        fail("expected one base vLLM Deployment")
        return
    doc = matches[0][1]
    spec = doc.get("spec") or {}
    pod = pod_spec(doc)
    vllm = None
    for container in containers(doc):
        if container.get("name") == "vllm":
            vllm = container
    if spec.get("replicas") != 0 or vllm is None or resource_gpu(vllm) != 1 or not has_gpu_toleration(pod):
        fail("vLLM replicas, GPU request, or toleration is wrong")
        return
    startup = vllm.get("startupProbe") or {}
    period = startup.get("periodSeconds")
    threshold = startup.get("failureThreshold")
    if not isinstance(period, int) or not isinstance(threshold, int) or period * threshold < 900:
        fail("vLLM startup probe covers less than 15 minutes")
        return
    grace = pod.get("terminationGracePeriodSeconds")
    pre_stop = ((vllm.get("lifecycle") or {}).get("preStop"))
    if not isinstance(grace, int) or grace < 90 or not pre_stop:
        fail("vLLM preStop or termination grace is missing")
        return
    say("vLLM replicas are 0, one GPU is requested, and the startup probe covers 15 minutes.")


def check_door(docs):
    matches = kind_named(docs, "Deployment", "door")
    if len(matches) != 1:
        fail("expected one base door Deployment")
        return
    doc = matches[0][1]
    pod = pod_spec(doc)
    if has_gpu_toleration(pod):
        fail("the door tolerates the GPU taint")
        return
    for container in containers(doc):
        resources = container.get("resources") or {}
        for section in ("requests", "limits"):
            if "nvidia.com/gpu" in (resources.get(section) or {}):
                fail("the door requests a GPU")
                return
    say("The door requests no GPU and carries no GPU toleration.")


def ingress_service_names(doc):
    names = []
    spec = doc.get("spec") or {}
    default = ((spec.get("defaultBackend") or {}).get("service") or {}).get("name")
    if isinstance(default, str):
        names.append(default)
    for rule in spec.get("rules") or []:
        http = (rule or {}).get("http") or {}
        for path in http.get("paths") or []:
            name = (((path.get("backend") or {}).get("service") or {}).get("name"))
            if isinstance(name, str):
                names.append(name)
    return names


def check_ingress(docs):
    ingresses = [doc for path, doc in docs if not overlay(path) and doc.get("kind") == "Ingress"]
    if len(ingresses) != 1:
        fail("expected one Ingress")
        return
    doc = ingresses[0]
    names = ingress_service_names(doc)
    attributes = ((doc.get("metadata") or {}).get("annotations") or {}).get(
        "alb.ingress.kubernetes.io/load-balancer-attributes",
        "",
    )
    if names != ["door"] or "vllm" in names or "idle_timeout.timeout_seconds=300" not in attributes:
        fail("the Ingress does not target only the door with a 300 second idle timeout")
        return
    say("The Ingress backend is the door, with a 300 second idle timeout.")


def scaled_query(doc):
    triggers = (doc.get("spec") or {}).get("triggers") or []
    parts = []
    for trigger in triggers:
        query = ((trigger or {}).get("metadata") or {}).get("query")
        if isinstance(query, str):
            parts.append(query)
    return "\n".join(parts)


def check_scaler(docs):
    matches = kind_named(docs, "ScaledObject", "vllm")
    if len(matches) != 1:
        fail("expected one base ScaledObject")
        return
    spec = matches[0][1].get("spec") or {}
    query = scaled_query(matches[0][1])
    missing = [name for name in BUSY if name not in query]
    if spec.get("minReplicaCount") != 0 or spec.get("maxReplicaCount") != 2 or missing:
        fail("the ScaledObject does not span 0 to 2 on the four busy metrics")
        return
    say("The ScaledObject spans 0 to 2 replicas and names all four busy metrics.")


def instance_types(doc):
    requirements = ((doc.get("spec") or {}).get("template") or {}).get("spec", {}).get("requirements") or []
    found = []
    for item in requirements:
        if (item or {}).get("key") == "node.kubernetes.io/instance-type":
            found.extend(item.get("values") or [])
    return found


def check_pools(docs):
    spot = kind_named(docs, "NodePool", "gpu-spot")
    demand = kind_named(docs, "NodePool", "gpu-ondemand")
    if len(spot) != 1 or len(demand) != 1:
        fail("expected one spot pool and one on-demand pool")
        return
    spot_doc = spot[0][1]
    demand_doc = demand[0][1]
    spot_weight = (spot_doc.get("spec") or {}).get("weight")
    demand_weight = (demand_doc.get("spec") or {}).get("weight")
    needed = {"g6.xlarge", "g5.xlarge"}
    if (
        not isinstance(spot_weight, int)
        or not isinstance(demand_weight, int)
        or spot_weight <= demand_weight
        or not needed.issubset(instance_types(spot_doc))
        or not needed.issubset(instance_types(demand_doc))
    ):
        fail("spot weight is not above on-demand, or an instance type is missing")
        return
    say("The spot pool outweighs on-demand, and both list g6.xlarge and g5.xlarge.")


def check_overlay(docs):
    scaled = None
    consolidated = []
    for path, doc in docs:
        if not overlay(path):
            continue
        if doc.get("kind") == "ScaledObject":
            scaled = doc
        if doc.get("kind") == "NodePool":
            after = (((doc.get("spec") or {}).get("disruption") or {}).get("consolidateAfter"))
            consolidated.append(after)
    spec = (scaled or {}).get("spec") or {}
    if spec.get("maxReplicaCount") != 1 or spec.get("cooldownPeriod") != 60 or consolidated.count("30s") < 2:
        fail("the window overlay does not cap replicas at 1 or shorten cooldown and consolidateAfter")
        return
    say("The window overlay caps replicas at 1, sets cooldown to 60, and consolidateAfter to 30s.")


def rule_text(rule):
    expr = rule.get("expr")
    if not isinstance(expr, str):
        return ""
    return " ".join(expr.split())


def check_rules(docs):
    rules = []
    for path, doc in docs:
        if overlay(path) or doc.get("kind") != "PrometheusRule":
            continue
        for group in (doc.get("spec") or {}).get("groups") or []:
            rules.extend(group.get("rules") or [])
    alerts = [rule for rule in rules if rule.get("alert")]
    texts = [(rule.get("for"), rule_text(rule).lower()) for rule in alerts]
    saturation = any(duration == "2m" and "door_requests_waiting" in text and "replicas" in text for duration, text in texts)
    cold = any(duration == "15m" and "door_requests_waiting" in text and "== 0" in text for duration, text in texts)
    cache = any(
        duration == "5m" and "0.90" in text and "waiting" in text and "gpu_cache_usage_perc" in text
        for duration, text in texts
    )
    pages_on_429 = any("429" in text for _duration, text in texts)
    if not saturation or not cold or not cache or pages_on_429:
        fail("Prometheus rules missed a page condition or a rule pages on 429")
        return
    say("Prometheus rules page on saturation, a stuck cold start, and KV cache pressure. None page on 429.")


def check_grafana():
    serving_path = DEPLOY / "grafana" / "serving.json"
    fleet_path = DEPLOY / "grafana" / "fleet.json"
    try:
        serving = serving_path.read_text(encoding="utf-8")
        fleet = fleet_path.read_text(encoding="utf-8")
        json.loads(serving)
        json.loads(fleet)
    except (OSError, json.JSONDecodeError) as exc:
        fail("Grafana JSON did not load (%s)" % exc)
        return
    serving_text = serving.lower()
    fleet_text = fleet.lower()
    if "ttft" not in serving_text or "kv cache" not in serving_text or "per thousand" in serving_text:
        fail("serving.json missed TTFT or KV cache, or it names a cost ratio")
        return
    if "cost per thousand" not in fleet_text:
        fail("fleet.json does not name cost per thousand")
        return
    say("serving.json names TTFT and KV cache. fleet.json names cost per thousand.")


def main():
    if not DEPLOY.is_dir():
        fail("deploy/ is missing")
        return 1
    docs = load_documents()
    run_kubeconform(docs)
    check_vllm(docs)
    check_door(docs)
    check_ingress(docs)
    check_scaler(docs)
    check_pools(docs)
    check_overlay(docs)
    check_rules(docs)
    check_grafana()
    if FAILURES:
        return 1
    say("Phase 4 passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
