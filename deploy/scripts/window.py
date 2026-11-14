"""Create the one-sitting GPU window, run the card, and delete the cluster.

Pull requests do not call this module. `run` returns before any AWS call unless
GPU_WINDOW_CONFIRM=yes and the price and bucket variables are set. `destroy`
deletes only the cluster recorded in .tools/gpu-window-state.json.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = ROOT / ".tools"
STATE = TOOLS / "gpu-window-state.json"
KUBECONFIG = TOOLS / "gpu-window.kubeconfig"
CLUSTER = "inference"
NAMESPACE = "inference"
KARPENTER_VERSION = "1.4.0"
KARPENTER_TEMPLATE = (
    "https://raw.githubusercontent.com/aws/karpenter-provider-aws/v%s/"
    "website/content/en/preview/getting-started/getting-started-with-karpenter/cloudformation.yaml"
    % KARPENTER_VERSION
)
LB_POLICY = (
    "https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/"
    "v2.11.0/docs/install/iam_policy.json"
)
PREFIX = "models/Meta-Llama-3.1-8B-Instruct"
PRICES = (
    "PRICE_G6_SPOT",
    "PRICE_G6_ONDEMAND",
    "PRICE_G5_SPOT",
    "PRICE_G5_ONDEMAND",
)


class WindowAbort(RuntimeError):
    pass


def say(text):
    print(text, flush=True)


def kube_env():
    env = os.environ.copy()
    env["KUBECONFIG"] = str(KUBECONFIG)
    return env


def run_cmd(args, timeout=300, check=True, env=None, cwd=None):
    say("+ " + " ".join(args))
    result = subprocess.run(
        args,
        cwd=str(cwd or ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WindowAbort(detail[-2000:] or "command failed")
    return result


def aws(args, timeout=120):
    region = os.environ["AWS_REGION"]
    result = run_cmd(["aws", "--no-cli-pager", "--region", region, "--output", "json", *args], timeout=timeout)
    text = result.stdout.strip()
    return json.loads(text) if text else {}


def kubectl(*args, timeout=180):
    return run_cmd(["kubectl", *args], timeout=timeout, env=kube_env()).stdout


def require_tools():
    missing = [name for name in ("aws", "eksctl", "helm", "kubectl", "docker") if shutil.which(name) is None]
    if missing:
        raise WindowAbort("install " + ", ".join(missing))


def require_env():
    missing = ["AWS_REGION", "WEIGHT_BUCKET", *PRICES]
    absent = [name for name in missing if not os.environ.get(name, "").strip()]
    bad = []
    for name in PRICES:
        raw = os.environ.get(name, "").strip()
        try:
            if float(raw) <= 0:
                bad.append(name)
        except ValueError:
            bad.append(name)
    if absent or bad:
        raise WindowAbort("set a positive value for " + ", ".join(absent + bad))


def write_state():
    TOOLS.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps({"cluster": CLUSTER, "region": os.environ["AWS_REGION"]}),
        encoding="utf-8",
    )


def read_state():
    if not STATE.is_file():
        return None
    payload = json.loads(STATE.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload.get("cluster") or not payload.get("region"):
        return None
    return payload


def account_id():
    payload = aws(["sts", "get-caller-identity"])
    return str(payload["Account"])


def assert_no_gpu():
    import stage_weights

    found = stage_weights.gpu_instances()
    if found:
        lines = ["%s %s is %s" % item for item in found]
        raise WindowAbort("a GPU instance is already up: " + "; ".join(lines))


def create_cluster():
    TOOLS.mkdir(parents=True, exist_ok=True)
    region = os.environ["AWS_REGION"]
    manifest = TOOLS / "eksctl.yaml"
    manifest.write_text(
        "\n".join([
            "apiVersion: eksctl.io/v1alpha5",
            "kind: ClusterConfig",
            "metadata:",
            "  name: %s" % CLUSTER,
            "  region: %s" % region,
            '  version: "1.32"',
            "  tags:",
            "    karpenter.sh/discovery: %s" % CLUSTER,
            "iam:",
            "  withOIDC: true",
            "addons:",
            "  - name: vpc-cni",
            "  - name: coredns",
            "  - name: kube-proxy",
            "  - name: eks-pod-identity-agent",
            "managedNodeGroups:",
            "  - name: cpu",
            "    instanceType: t3.large",
            "    desiredCapacity: 1",
            "    minSize: 1",
            "    maxSize: 2",
            "    amiFamily: AmazonLinux2023",
            "    tags:",
            "      karpenter.sh/discovery: %s" % CLUSTER,
            "",
        ]),
        encoding="utf-8",
    )
    run_cmd(
        ["eksctl", "create", "cluster", "-f", str(manifest), "--kubeconfig", str(KUBECONFIG)],
        timeout=1800,
    )


def install_karpenter():
    template = TOOLS / "karpenter-cloudformation.yaml"
    urllib.request.urlretrieve(KARPENTER_TEMPLATE, template)
    region = os.environ["AWS_REGION"]
    run_cmd(
        [
            "aws", "--no-cli-pager", "--region", region,
            "cloudformation", "deploy",
            "--stack-name", "Karpenter-%s" % CLUSTER,
            "--template-file", str(template),
            "--capabilities", "CAPABILITY_NAMED_IAM",
            "--parameter-overrides", "ClusterName=%s" % CLUSTER,
        ],
        timeout=600,
    )
    policy = "arn:aws:iam::%s:policy/KarpenterControllerPolicy-%s" % (account_id(), CLUSTER)
    run_cmd(
        [
            "eksctl", "create", "podidentityassociation",
            "--cluster", CLUSTER,
            "--namespace", "kube-system",
            "--service-account-name", "karpenter",
            "--role-name", "%s-karpenter" % CLUSTER,
            "--permission-policy-arn", policy,
            "--region", region,
        ],
        timeout=300,
    )
    run_cmd(["helm", "registry", "logout", "public.ecr.aws"], check=False)
    run_cmd(
        [
            "helm", "upgrade", "--install", "karpenter",
            "oci://public.ecr.aws/karpenter/karpenter",
            "--version", KARPENTER_VERSION,
            "--namespace", "kube-system", "--create-namespace",
            "--set", "settings.clusterName=%s" % CLUSTER,
            "--set", "settings.interruptionQueue=%s" % CLUSTER,
            "--wait", "--timeout", "10m",
        ],
        timeout=700,
        env=kube_env(),
    )


def install_load_balancer():
    policy_path = TOOLS / "lb-policy.json"
    urllib.request.urlretrieve(LB_POLICY, policy_path)
    region = os.environ["AWS_REGION"]
    account = account_id()
    policy_arn = "arn:aws:iam::%s:policy/AWSLoadBalancerControllerIAMPolicy" % account
    exists = run_cmd(
        ["aws", "--region", region, "iam", "get-policy", "--policy-arn", policy_arn],
        check=False,
    )
    if exists.returncode != 0:
        run_cmd(
            [
                "aws", "--no-cli-pager", "--region", region,
                "iam", "create-policy",
                "--policy-name", "AWSLoadBalancerControllerIAMPolicy",
                "--policy-document", "file://%s" % policy_path,
            ]
        )
    run_cmd(
        [
            "eksctl", "create", "iamserviceaccount",
            "--cluster", CLUSTER,
            "--region", region,
            "--namespace", "kube-system",
            "--name", "aws-load-balancer-controller",
            "--attach-policy-arn", policy_arn,
            "--override-existing-serviceaccounts",
            "--approve",
        ],
        timeout=300,
    )
    run_cmd(["helm", "repo", "add", "eks", "https://aws.github.io/eks-charts"], check=False)
    run_cmd(["helm", "repo", "update"], timeout=180)
    run_cmd(
        [
            "helm", "upgrade", "--install", "aws-load-balancer-controller",
            "eks/aws-load-balancer-controller",
            "--version", "1.11.0",
            "--namespace", "kube-system",
            "--set", "clusterName=%s" % CLUSTER,
            "--set", "serviceAccount.create=false",
            "--set", "serviceAccount.name=aws-load-balancer-controller",
            "--set", "region=%s" % region,
            "--set", "vpcId=%s" % vpc_id(),
            "--wait", "--timeout", "10m",
        ],
        timeout=700,
        env=kube_env(),
    )


def vpc_id():
    payload = aws(["eks", "describe-cluster", "--name", CLUSTER])
    return payload["cluster"]["resourcesVpcConfig"]["vpcId"]


def install_keda():
    run_cmd(["helm", "repo", "add", "kedacore", "https://kedacore.github.io/charts"], check=False)
    run_cmd(["helm", "repo", "update"], timeout=180)
    run_cmd(
        [
            "helm", "upgrade", "--install", "keda", "kedacore/keda",
            "--version", "2.17.2",
            "--namespace", "keda", "--create-namespace",
            "--wait", "--timeout", "10m",
        ],
        timeout=700,
        env=kube_env(),
    )


def install_prometheus():
    run_cmd(
        ["helm", "repo", "add", "prometheus-community", "https://prometheus-community.github.io/helm-charts"],
        check=False,
    )
    run_cmd(["helm", "repo", "update"], timeout=180)
    run_cmd(
        [
            "helm", "upgrade", "--install", "monitoring",
            "prometheus-community/kube-prometheus-stack",
            "--version", "69.8.2",
            "--namespace", "monitoring", "--create-namespace",
            "--set", "grafana.enabled=true",
            "--set", "prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false",
            "--set", "prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false",
            "--set", "prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false",
            "--wait", "--timeout", "12m",
        ],
        timeout=800,
        env=kube_env(),
    )
    alias_prometheus()
    for name in ("fleet", "serving"):
        source = ROOT / "deploy" / "grafana" / ("%s.json" % name)
        labeled = run_cmd(
            [
                "kubectl", "create", "configmap", "grafana-%s" % name,
                "--from-file=%s.json=%s" % (name, source),
                "-n", "monitoring",
                "--dry-run=client", "-o", "yaml",
            ],
            env=kube_env(),
        ).stdout
        labeled = labeled.replace(
            "  name: grafana-%s\n" % name,
            "  name: grafana-%s\n  labels:\n    grafana_dashboard: \"1\"\n" % name,
            1,
        )
        applied = TOOLS / ("grafana-%s.yaml" % name)
        applied.write_text(labeled, encoding="utf-8")
        run_cmd(["kubectl", "apply", "-f", str(applied)], env=kube_env())


def alias_prometheus():
    payload = json.loads(kubectl("get", "svc", "-n", "monitoring", "-o", "json"))
    chosen = None
    for item in payload.get("items") or []:
        name = item["metadata"]["name"]
        if "prometheus" in name and "alertmanager" not in name and "operator" not in name:
            chosen = item
            break
    if chosen is None:
        raise WindowAbort("Prometheus service was not installed")
    if chosen["metadata"]["name"] == "prometheus":
        return
    port = chosen["spec"]["ports"][0]
    manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "prometheus", "namespace": "monitoring"},
        "spec": {
            "selector": chosen["spec"]["selector"],
            "ports": [{
                "name": "http",
                "port": 9090,
                "targetPort": port.get("targetPort") or port.get("port"),
            }],
        },
    }
    path = TOOLS / "prometheus-alias.yaml"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    run_cmd(["kubectl", "apply", "-f", str(path)], env=kube_env())


def ensure_weight_role():
    region = os.environ["AWS_REGION"]
    account = account_id()
    bucket = os.environ["WEIGHT_BUCKET"]
    cluster = aws(["eks", "describe-cluster", "--name", CLUSTER])["cluster"]
    issuer = cluster["identity"]["oidc"]["issuer"].replace("https://", "")
    provider = "arn:aws:iam::%s:oidc-provider/%s" % (account, issuer)
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Federated": provider},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {"StringEquals": {
                "%s:sub" % issuer: "system:serviceaccount:inference:vllm",
                "%s:aud" % issuer: "sts.amazonaws.com",
            }},
        }],
    }
    trust_path = TOOLS / "weight-trust.json"
    trust_path.write_text(json.dumps(trust), encoding="utf-8")
    role = "inference-vllm-weights"
    exists = run_cmd(["aws", "--region", region, "iam", "get-role", "--role-name", role], check=False)
    if exists.returncode != 0:
        run_cmd([
            "aws", "--region", region, "iam", "create-role",
            "--role-name", role,
            "--assume-role-policy-document", "file://%s" % trust_path,
        ])
    else:
        run_cmd([
            "aws", "--region", region, "iam", "update-assume-role-policy",
            "--role-name", role,
            "--policy-document", "file://%s" % trust_path,
        ])
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": "arn:aws:s3:::%s/%s/*" % (bucket, PREFIX),
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": "arn:aws:s3:::%s" % bucket,
                "Condition": {"StringLike": {"s3:prefix": [PREFIX, PREFIX + "/*"]}},
            },
        ],
    }
    policy_path = TOOLS / "weight-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    run_cmd([
        "aws", "--region", region, "iam", "put-role-policy",
        "--role-name", role,
        "--policy-name", "weights-read",
        "--policy-document", "file://%s" % policy_path,
    ])
    arn = "arn:aws:iam::%s:role/%s" % (account, role)
    kubectl(
        "annotate", "serviceaccount", "vllm", "-n", NAMESPACE,
        "eks.amazonaws.com/role-arn=%s" % arn, "--overwrite",
    )
    return arn


def push_door():
    account = account_id()
    region = os.environ["AWS_REGION"]
    repo = "%s.dkr.ecr.%s.amazonaws.com/inference-door" % (account, region)
    exists = run_cmd(
        ["aws", "--region", region, "ecr", "describe-repositories", "--repository-names", "inference-door"],
        check=False,
    )
    if exists.returncode != 0:
        run_cmd(["aws", "--region", region, "ecr", "create-repository", "--repository-name", "inference-door"])
    password = run_cmd(["aws", "--region", region, "ecr", "get-login-password"]).stdout.strip()
    login = subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", "%s.dkr.ecr.%s.amazonaws.com" % (account, region)],
        input=password,
        text=True,
        cwd=str(ROOT),
    )
    if login.returncode != 0:
        raise WindowAbort("ECR login failed")
    image = repo + ":local"
    run_cmd(["docker", "build", "-f", "Dockerfile.door", "-t", image, "."], timeout=600)
    run_cmd(["docker", "push", image], timeout=600)
    return image


def apply_window(image):
    bucket = os.environ["WEIGHT_BUCKET"]
    data = {
        "bucket": bucket,
        "priceG6Spot": os.environ["PRICE_G6_SPOT"],
        "priceG6OnDemand": os.environ["PRICE_G6_ONDEMAND"],
        "priceG5Spot": os.environ["PRICE_G5_SPOT"],
        "priceG5OnDemand": os.environ["PRICE_G5_ONDEMAND"],
    }
    pieces = ["--from-literal=%s=%s" % item for item in data.items()]
    run_cmd(
        ["kubectl", "create", "configmap", "inference-window", "-n", NAMESPACE, *pieces, "--dry-run=client", "-o", "yaml"],
        env=kube_env(),
    )
    rendered = run_cmd(
        ["kubectl", "create", "configmap", "inference-window", "-n", NAMESPACE, *pieces, "--dry-run=client", "-o", "yaml"],
        env=kube_env(),
    ).stdout
    path = TOOLS / "inference-window.yaml"
    path.write_text(rendered, encoding="utf-8")
    run_cmd(["kubectl", "apply", "-k", "deploy/overlays/window"], timeout=180, env=kube_env())
    run_cmd(["kubectl", "apply", "-f", str(path)], env=kube_env())
    run_cmd(
        ["kubectl", "set", "image", "deployment/door", "door=%s" % image, "-n", NAMESPACE],
        env=kube_env(),
    )


def gpu_nodes():
    payload = json.loads(kubectl("get", "nodes", "-o", "json"))
    found = []
    for item in payload.get("items") or []:
        labels = (item.get("metadata") or {}).get("labels") or {}
        if labels.get("inference.ai/gpu") != "true":
            continue
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in (item.get("status") or {}).get("conditions") or []
        )
        found.append({"name": item["metadata"]["name"], "ready": ready})
    return found


def vllm_settled():
    spec = kubectl("get", "deploy", "vllm", "-n", NAMESPACE, "-o", "jsonpath={.spec.replicas}").strip()
    ready = kubectl("get", "deploy", "vllm", "-n", NAMESPACE, "-o", "jsonpath={.status.readyReplicas}").strip()
    replicas = int(spec or "0")
    ready_count = int(ready or "0")
    return replicas == 0 and ready_count == 0 and not gpu_nodes()


def vllm_pod_ready():
    payload = json.loads(kubectl("get", "pods", "-n", NAMESPACE, "-l", "app=vllm", "-o", "json"))
    for item in payload.get("items") or []:
        if item.get("status", {}).get("phase") == "Running":
            for status in item["status"].get("containerStatuses") or []:
                if status.get("name") == "vllm" and status.get("ready"):
                    return True
    return False


def guard(stop, abort):
    ready_since = {}
    while not stop.is_set():
        try:
            nodes = gpu_nodes()
        except (WindowAbort, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            abort.append(str(exc))
            return
        if len(nodes) > 1:
            abort.append("a second GPU node appeared")
            return
        now = time.time()
        for node in nodes:
            if node["ready"]:
                ready_since.setdefault(node["name"], now)
                if now - ready_since[node["name"]] > 20 * 60:
                    abort.append("the GPU node has been Ready for 20 minutes")
                    return
        if nodes and nodes[0]["ready"] and not vllm_pod_ready():
            started = ready_since.get(nodes[0]["name"])
            if started and now - started > 15 * 60:
                abort.append("startup is still failing 15 minutes after the node is Ready")
                return
        stop.wait(15)


def ingress_host():
    deadline = time.time() + 600
    while time.time() < deadline:
        host = kubectl(
            "get", "ingress", "door", "-n", NAMESPACE,
            "-o", "jsonpath={.status.loadBalancer.ingress[0].hostname}",
        ).strip()
        if host:
            return host
        time.sleep(10)
    raise WindowAbort("the load balancer hostname did not appear")


def chat(host, messages, max_tokens, timeout):
    import http.client

    body = json.dumps({
        "model": "llama-3.1-8b-instruct",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Authorization": "Bearer fixture-beta",
    }
    started = time.perf_counter()
    ttft = None
    chunks = []
    conn = http.client.HTTPConnection(host, 80, timeout=timeout)
    try:
        conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
        resp = conn.getresponse()
        while True:
            piece = resp.read(4096)
            if not piece:
                break
            chunks.append(piece)
            if ttft is None and b'"content":' in b"".join(chunks):
                ttft = (time.perf_counter() - started) * 1000
        ended = time.perf_counter()
        return {
            "status": resp.status,
            "body": b"".join(chunks),
            "ttftMs": ttft,
            "latencyMs": (ended - started) * 1000,
            "started": started,
            "ended": ended,
        }
    finally:
        conn.close()


def run_card(host):
    import harness
    import workload

    cold_started = time.perf_counter()
    cold = chat(host, [{"role": "user", "content": "Reply with the word ready."}], 16, 16 * 60)
    cold_ms = (time.perf_counter() - cold_started) * 1000
    if cold["status"] != 200:
        raise WindowAbort("cold request returned %s" % cold["status"])
    cold_text = harness.format_cold(round(cold_ms, 1))
    if "p50" in cold_text:
        raise WindowAbort("cold report included a warm percentile")
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "cold.txt").write_text(cold_text, encoding="utf-8")
    say(cold_text.rstrip("\n"))

    batch = workload.card_batch()
    rows = [None] * len(batch)

    def one(index, item):
        result = chat(host, item["messages"], item["max_tokens"], 180)
        content, usage = harness.parse_sse(result["body"]) if result["status"] == 200 else ("", None)
        rows[index] = {
            "status": result["status"],
            "latencyMs": result["latencyMs"],
            "ttftMs": result["ttftMs"],
            "tokens": harness.completion_count(usage),
            "started": result["started"],
            "ended": result["ended"],
            "sawContent": bool(content.strip()),
        }

    threads = [threading.Thread(target=one, args=(index, item)) for index, item in enumerate(batch)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if any(row is None or row["status"] != 200 or not row["sawContent"] or row["ttftMs"] is None for row in rows):
        raise WindowAbort("warm card request did not complete")
    span = max(row["ended"] for row in rows) - min(row["started"] for row in rows)
    if span <= 0:
        raise WindowAbort("warm card had no elapsed time")
    matched = 0
    for item in workload.FORMAT_SET:
        result = chat(host, item["messages"], item["max_tokens"], 180)
        content, _usage = harness.parse_sse(result["body"]) if result["status"] == 200 else ("", None)
        if workload.shape_matches(content, item):
            matched += 1
    report = {
        "p50Ms": round(harness.percentile([row["latencyMs"] for row in rows], 50), 1),
        "p95Ms": round(harness.percentile([row["latencyMs"] for row in rows], 95), 1),
        "outputTokensPerSecond": round(sum(row["tokens"] or 0 for row in rows) / span, 2),
        "clientTimeToFirstTokenMs": round(harness.percentile([row["ttftMs"] for row in rows], 50), 1),
        "qualityMatched": matched,
    }
    lines = [harness.format_warm(report).rstrip("\n")]
    lines.extend(engine_lines())
    warm_text = "\n".join(lines) + "\n"
    (ROOT / "reports" / "warm.txt").write_text(warm_text, encoding="utf-8")
    say(warm_text.rstrip("\n"))


def prom_query(expr):
    import urllib.parse

    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", "monitoring", "svc/prometheus", "19090:9090"],
        env=kube_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        url = "http://127.0.0.1:19090/api/v1/query?" + urllib.parse.urlencode({"query": expr})
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                result = ((payload.get("data") or {}).get("result")) or []
                if result:
                    return result[0]["value"][1]
            except (OSError, json.JSONDecodeError, KeyError, IndexError):
                time.sleep(1)
        return None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def engine_lines():
    names = {
        "engine TTFT p50": "histogram_quantile(0.5, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[15m])))",
        "engine inter-token latency p50": "histogram_quantile(0.5, sum by (le) (rate(vllm:time_per_output_token_seconds_bucket[15m])))",
        "engine queue wait p50": "histogram_quantile(0.5, sum by (le) (rate(vllm:request_queue_time_seconds_bucket[15m])))",
    }
    lines = []
    for label, expr in names.items():
        value = prom_query(expr)
        if value is None:
            raise WindowAbort(label + " was absent from Prometheus")
        lines.append("%s: %ss" % (label, value))
    cache = prom_query("max(vllm:gpu_cache_usage_perc)")
    if cache is None:
        cache = prom_query("max(vllm:kv_cache_usage_perc)")
    if cache is None:
        raise WindowAbort("the KV cache gauge was absent")
    lines.append("KV cache: %s" % cache)
    return lines


def wait_until_idle(stop, abort):
    deadline = time.time() + 15 * 60
    while time.time() < deadline:
        if abort:
            raise WindowAbort(abort[0])
        if vllm_settled():
            say("Ready replicas are 0 and no GPU node remains.")
            return
        time.sleep(10)
    raise WindowAbort("replicas or the GPU node were still up after the cooldown")


def run():
    if os.environ.get("GPU_WINDOW_CONFIRM") != "yes":
        say("Set GPU_WINDOW_CONFIRM=yes to start the GPU window. This command did not create a cluster.")
        return 2
    try:
        require_env()
        require_tools()
        assert_no_gpu()
        write_state()
        create_cluster()
        install_karpenter()
        install_load_balancer()
        install_keda()
        install_prometheus()
        image = push_door()
        apply_window(image)
        ensure_weight_role()
        stop = threading.Event()
        abort = []
        watcher = threading.Thread(target=guard, args=(stop, abort), daemon=True)
        watcher.start()
        try:
            host = ingress_host()
            say("Load balancer: %s" % host)
            run_card(host)
            wait_until_idle(stop, abort)
        finally:
            stop.set()
            watcher.join(timeout=5)
        if abort:
            raise WindowAbort(abort[0])
    except (WindowAbort, subprocess.TimeoutExpired, OSError, json.JSONDecodeError, KeyError) as exc:
        say("Window aborted: %s" % exc)
        return 1
    say("The window card finished. Destroy still deletes the cluster.")
    return 0


def destroy():
    state = read_state()
    if state is None:
        say("No GPU window cluster is recorded.")
        return 0
    if shutil.which("eksctl") is None:
        say("eksctl is not installed. The recorded cluster is %s in %s." % (state["cluster"], state["region"]))
        return 1
    result = subprocess.run(
        [
            "eksctl", "delete", "cluster",
            "--name", state["cluster"],
            "--region", state["region"],
            "--wait",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    detail = ((result.stderr or "") + (result.stdout or "")).lower()
    if result.returncode != 0 and "not found" not in detail and "does not exist" not in detail:
        say((result.stderr or result.stdout or "delete failed").strip()[-2000:])
        return 1
    STATE.unlink(missing_ok=True)
    say("Deleted cluster %s." % state["cluster"])
    return 0
