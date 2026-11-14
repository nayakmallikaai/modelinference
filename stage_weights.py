"""Phase 7 gate. Stage Llama weights to S3 from this CPU and read config.json back.

No GPU instance is started. A running or pending g4, g5, g6, or p instance
fails the phase. Set MODEL_DIR to a local model directory when the bucket
does not already hold the shards. WEIGHT_BUCKET overrides the bucket name.
AWS_REGION selects the region. The fallback region is us-east-1.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PREFIX = "models/Meta-Llama-3.1-8B-Instruct"
MIN_BYTES = 15 * 10**9
REQUIRED = ("config.json", "tokenizer.json", "tokenizer_config.json")
ALLOWED = {
    ("sts", "get-caller-identity"),
    ("ec2", "describe-instances"),
    ("s3api", "head-bucket"),
    ("s3api", "create-bucket"),
    ("s3api", "put-public-access-block"),
    ("s3api", "put-bucket-encryption"),
    ("s3api", "list-objects-v2"),
    ("s3api", "get-object"),
    ("s3", "sync"),
}
GPU_PREFIXES = ("g4", "g5", "g6", "p")

FAILURES = []


def say(text):
    print(text, flush=True)


def fail(message):
    FAILURES.append(message)
    say("Check failed: " + message)


class CommandFailed(RuntimeError):
    pass


def region_name():
    for key in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return "us-east-1"


def aws(args, timeout=300, as_json=True):
    pair = tuple(args[:2])
    if pair not in ALLOWED:
        raise CommandFailed("blocked aws call")
    command = ["aws", "--no-cli-pager", "--region", region_name()]
    if as_json:
        command.extend(["--output", "json"])
    command.extend(args)
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "aws failed").strip()
        raise CommandFailed(detail)
    if not as_json:
        return result.stdout
    text = result.stdout.strip()
    if not text:
        return {}
    return json.loads(text)


def aws_status(args):
    pair = tuple(args[:2])
    if pair not in ALLOWED:
        raise CommandFailed("blocked aws call")
    command = ["aws", "--no-cli-pager", "--region", region_name(), "--output", "json", *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    return result.returncode, (result.stderr or "") + (result.stdout or "")


def gpu_family(instance_type):
    family = instance_type.split(".", 1)[0]
    return family.startswith(GPU_PREFIXES)


def gpu_instances():
    payload = aws(
        [
            "ec2",
            "describe-instances",
            "--filters",
            "Name=instance-state-name,Values=pending,running",
        ]
    )
    found = []
    for reservation in payload.get("Reservations") or []:
        for instance in reservation.get("Instances") or []:
            instance_type = instance.get("InstanceType") or ""
            if gpu_family(instance_type):
                found.append((instance.get("InstanceId") or "", instance_type, (instance.get("State") or {}).get("Name") or ""))
    return found


def require_no_gpu():
    found = gpu_instances()
    if not found:
        say("No running or pending g4, g5, g6, or p instance in %s." % region_name())
        return True
    for instance_id, instance_type, state in found:
        say("%s %s is %s" % (instance_id, instance_type, state))
    fail("A GPU instance is up. Terminate it before phase 8.")
    return False


def bucket_missing(bucket):
    code, detail = aws_status(["s3api", "head-bucket", "--bucket", bucket])
    if code == 0:
        return False
    if "404" in detail or "Not Found" in detail or "NoSuchBucket" in detail:
        return True
    raise CommandFailed(detail.strip() or "head-bucket failed")


def list_objects(bucket):
    token = None
    found = []
    while True:
        args = ["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", PREFIX + "/"]
        if token:
            args.extend(["--continuation-token", token])
        payload = aws(args)
        found.extend(payload.get("Contents") or [])
        if not payload.get("IsTruncated"):
            return found
        token = payload.get("NextContinuationToken")
        if not token:
            return found


def object_report(objects):
    names = {item["Key"].split("/")[-1] for item in objects if item.get("Key")}
    missing = [name for name in REQUIRED if name not in names]
    shards = [item["Key"] for item in objects if str(item.get("Key", "")).endswith(".safetensors")]
    total = sum(int(item.get("Size") or 0) for item in objects)
    return missing, shards, total


def ready(objects):
    missing, shards, total = object_report(objects)
    return not missing and bool(shards) and total >= MIN_BYTES


def local_root():
    raw = os.environ.get("MODEL_DIR", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_dir():
        fail("MODEL_DIR is not a directory")
        return None
    if (path / "config.json").is_file():
        return path
    found = [item for item in path.rglob("config.json") if item.is_file()]
    if len(found) == 1:
        return found[0].parent
    fail("MODEL_DIR does not contain one config.json")
    return None


def local_ready(path):
    names = set()
    seen = set()
    total = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        names.add(item.name)
        resolved = item.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        total += resolved.stat().st_size
    missing = [name for name in REQUIRED if name not in names]
    shards = [name for name in names if name.endswith(".safetensors")]
    if missing or not shards or total < MIN_BYTES:
        fail("MODEL_DIR is missing shards or is under 15 GB")
        return False
    return True


def create_bucket(bucket):
    region = region_name()
    args = ["s3api", "create-bucket", "--bucket", bucket]
    if region != "us-east-1":
        args.extend(["--create-bucket-configuration", "LocationConstraint=%s" % region])
    aws(args, as_json=True)
    aws(
        [
            "s3api",
            "put-public-access-block",
            "--bucket",
            bucket,
            "--public-access-block-configuration",
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
        ],
        as_json=True,
    )
    aws(
        [
            "s3api",
            "put-bucket-encryption",
            "--bucket",
            bucket,
            "--server-side-encryption-configuration",
            '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}',
        ],
        as_json=True,
    )


def sync_model(bucket, path):
    destination = "s3://%s/%s/" % (bucket, PREFIX)
    aws(["s3", "sync", str(path), destination], timeout=7200, as_json=False)


def read_config(bucket):
    handle = tempfile.NamedTemporaryFile(prefix="llama-config-", suffix=".json", delete=False)
    handle.close()
    destination = Path(handle.name)
    try:
        aws(
            [
                "s3api",
                "get-object",
                "--bucket",
                bucket,
                "--key",
                PREFIX + "/config.json",
                str(destination),
            ],
            as_json=True,
        )
        payload = json.loads(destination.read_text(encoding="utf-8"))
    finally:
        destination.unlink(missing_ok=True)
    if not isinstance(payload, dict):
        fail("config.json from the bucket is not an object")
        return False
    kind = payload.get("model_type") or payload.get("architectures") or "object"
    say("A CPU process read config.json back from the bucket (%s)." % kind)
    return True


def account_bucket():
    explicit = os.environ.get("WEIGHT_BUCKET", "").strip()
    if explicit:
        return explicit
    identity = aws(["sts", "get-caller-identity"])
    account = str(identity.get("Account") or "").strip()
    if not account.isdigit():
        raise CommandFailed("the caller account was not a number")
    return "inference-weights-" + account


def main():
    if shutil.which("aws") is None:
        fail("the aws cli is not installed")
        return 1
    try:
        if not require_no_gpu():
            return 1
        bucket = account_bucket()
        say("Weight bucket: %s" % bucket)
        say("Prefix: %s" % PREFIX)
        missing_bucket = bucket_missing(bucket)
        objects = [] if missing_bucket else list_objects(bucket)
        if not ready(objects):
            root = local_root()
            if root is None or not local_ready(root):
                if not FAILURES:
                    fail("the bucket does not yet hold config.json, the tokenizer, and at least 15 GB of shards")
                return 1
            if missing_bucket:
                create_bucket(bucket)
            sync_model(bucket, root)
            objects = list_objects(bucket)
        missing, shards, total = object_report(objects)
        if missing or not shards or total < MIN_BYTES:
            fail("the bucket prefix is incomplete (%.2f GB)" % (total / 10**9))
            return 1
        say(
            "The prefix has config.json, the tokenizer files, %d weight shards, and %.2f GB."
            % (len(shards), total / 10**9)
        )
        if not read_config(bucket):
            return 1
        if not require_no_gpu():
            return 1
    except (CommandFailed, subprocess.TimeoutExpired, OSError, json.JSONDecodeError, KeyError) as exc:
        fail(str(exc))
        return 1
    if FAILURES:
        return 1
    say("Phase 7 passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
