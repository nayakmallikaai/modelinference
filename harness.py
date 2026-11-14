"""Print a warm report and a separate cold report from the local edge.

Start the edge first, from this folder:

    python3 serve.py

Then:

    python3 harness.py
"""

import http.client
import json
import re
import socket
import threading
import time
from decimal import Decimal
from pathlib import Path

import workload

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
EDGE = ("127.0.0.1", 8787)
FAILURES = []


def say(text):
    print(text, flush=True)


def fail(message):
    FAILURES.append(message)
    say("Check failed: " + message)


def exchange(method, path, body=None, headers=None, timeout=30, port=8787):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.connect()
        try:
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read()
        return resp.status, payload
    finally:
        conn.close()


def edge_up():
    try:
        status, _ = exchange("GET", "/health", timeout=2)
    except OSError:
        return False
    return status == 200


def post_json(path, payload, credential=None, timeout=30):
    raw = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}
    if credential:
        headers["Authorization"] = "Bearer " + credential
    return exchange("POST", path, body=raw, headers=headers, timeout=timeout)


def get_json(path):
    status, body = exchange("GET", path, timeout=5)
    if status != 200:
        raise RuntimeError("GET %s returned %s" % (path, status))
    return json.loads(body.decode("utf-8"))


def set_ready(count):
    status, body = post_json("/api/ready", {"readyReplicas": count})
    if status != 200:
        raise RuntimeError("ready")
    return json.loads(body.decode("utf-8"))["readyReplicas"]


def wait_until(predicate, seconds):
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        state = get_json("/api/state")
        if predicate(state):
            return state
        time.sleep(0.02)
    return None


def chat_body(messages, max_tokens, stream):
    return json.dumps({
        "model": "fixture-model",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }).encode("utf-8")


def buffered_chat(messages, max_tokens, credential="fixture-beta"):
    raw = chat_body(messages, max_tokens, False)
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        "Authorization": "Bearer " + credential,
    }
    started = time.perf_counter()
    status, body = exchange("POST", "/v1/chat/completions", body=raw, headers=headers)
    ended = time.perf_counter()
    return status, body, (ended - started) * 1000


def stream_chat(messages, max_tokens, credential="fixture-beta", on_content=None):
    raw = chat_body(messages, max_tokens, True)
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        "Authorization": "Bearer " + credential,
    }
    conn = http.client.HTTPConnection(EDGE[0], EDGE[1], timeout=30)
    started = time.perf_counter()
    try:
        conn.connect()
        try:
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        conn.request("POST", "/v1/chat/completions", body=raw, headers=headers)
        resp = conn.getresponse()
        buf = b""
        ttft = None
        while True:
            piece = resp.fp.read1(4096)
            if not piece:
                break
            buf += piece
            if ttft is None and b'"content":' in buf:
                ttft = (time.perf_counter() - started) * 1000
                if on_content is not None:
                    on_content()
                    on_content = None
        ended = time.perf_counter()
        return {
            "status": resp.status,
            "body": buf,
            "ttftMs": ttft,
            "latencyMs": (ended - started) * 1000,
            "started": started,
            "ended": ended,
        }
    finally:
        conn.close()


def parse_sse(raw):
    contents = []
    usage = None
    for block in raw.decode("utf-8", errors="replace").split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            payload = json.loads(data)
            delta = (payload.get("choices") or [{}])[0].get("delta") or {}
            if delta.get("content"):
                contents.append(delta["content"])
            if isinstance(payload.get("usage"), dict):
                usage = payload["usage"]
    return "".join(contents), usage


def completion_count(usage):
    if not isinstance(usage, dict):
        return None
    value = usage.get("completion_tokens")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def percentile(values, pct):
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def card_lines():
    streaming = "yes" if workload.STREAMING else "no"
    return [
        "Workload card",
        "  concurrency: %d" % workload.CONCURRENCY,
        "  prompt lengths: %s" % ", ".join(str(n) for n in workload.PROMPT_LENGTHS),
        "  output lengths: %s" % ", ".join(str(n) for n in workload.OUTPUT_LENGTHS),
        "  streaming: %s" % streaming,
    ]


def measure_card(item):
    if workload.STREAMING:
        result = stream_chat(item["messages"], item["max_tokens"])
        content, usage = parse_sse(result["body"]) if result["status"] == 200 else ("", None)
        return {
            "status": result["status"],
            "latencyMs": result["latencyMs"],
            "ttftMs": result["ttftMs"],
            "tokens": completion_count(usage),
            "started": result["started"],
            "ended": result["ended"],
            "sawContent": bool(content.strip()) if isinstance(content, str) else False,
        }
    status, body, latency = buffered_chat(item["messages"], item["max_tokens"])
    ended = time.perf_counter()
    content = ""
    usage = None
    if status == 200:
        payload = json.loads(body.decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage")
    return {
        "status": status,
        "latencyMs": latency,
        "ttftMs": latency,
        "tokens": completion_count(usage),
        "started": ended - (latency / 1000),
        "ended": ended,
        "sawContent": bool(content.strip()) if isinstance(content, str) else False,
    }


def run_format_check():
    matched = 0
    for item in workload.FORMAT_SET:
        status, body, _latency = buffered_chat(item["messages"], item["max_tokens"])
        if status != 200:
            continue
        payload = json.loads(body.decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        if workload.shape_matches(content, item):
            matched += 1
    return matched


def run_warm():
    set_ready(workload.REPLICA_CEILING)
    batch = workload.card_batch()
    if len(batch) != workload.CONCURRENCY:
        fail("frozen card batch does not match concurrency")
        return None
    sink = []
    lock = threading.Lock()

    def run(item):
        try:
            row = measure_card(item)
        except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            row = {"status": 0}
        with lock:
            sink.append(row)

    threads = [threading.Thread(target=run, args=(item,)) for item in batch]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if len(sink) != len(batch) or any(row.get("status") != 200 or not row.get("sawContent") for row in sink):
        fail("warm card request did not complete")
        return None
    if any(row.get("ttftMs") is None or row.get("tokens") is None for row in sink):
        fail("warm card did not report client time to first token and output tokens")
        return None
    expected = sorted(item["max_tokens"] for item in batch)
    if sorted(row["tokens"] for row in sink) != expected:
        fail("output lengths did not match the frozen card")
        return None
    span = max(row["ended"] for row in sink) - min(row["started"] for row in sink)
    if span <= 0:
        fail("warm card had no elapsed time")
        return None
    matched = run_format_check()
    if matched != len(workload.FORMAT_SET):
        fail("a format reply did not match the asked shape")
    return {
        "p50Ms": round(percentile([row["latencyMs"] for row in sink], 50), 1),
        "p95Ms": round(percentile([row["latencyMs"] for row in sink], 95), 1),
        "outputTokensPerSecond": round(sum(row["tokens"] for row in sink) / span, 2),
        "clientTimeToFirstTokenMs": round(percentile([row["ttftMs"] for row in sink], 50), 1),
        "qualityMatched": matched,
    }


def format_warm(report):
    lines = ["Warm report"]
    lines.extend(card_lines())
    lines.extend([
        "p50: %.1f ms" % report["p50Ms"],
        "p95: %.1f ms" % report["p95Ms"],
        "output tokens per second: %.2f" % report["outputTokensPerSecond"],
        "client time to first token: %.1f ms" % report["clientTimeToFirstTokenMs"],
        "pass count: %d" % report["qualityMatched"],
        "The pass count is the format check from this same run.",
    ])
    return "\n".join(lines) + "\n"


def format_cold(elapsed_ms):
    lines = ["Cold report"]
    lines.extend(card_lines())
    lines.extend([
        "time until first success: %.1f ms" % elapsed_ms,
        "Cold time is not mixed into the warm percentiles.",
    ])
    return "\n".join(lines) + "\n"


def post_report(payload):
    status, body = post_json("/api/report", payload)
    if status != 200:
        raise RuntimeError("report")
    return json.loads(body.decode("utf-8"))


def run_overlap():
    set_ready(workload.REPLICA_CEILING)
    box = {}
    first = threading.Event()
    done = threading.Event()

    def long_job():
        try:
            result = stream_chat(
                workload.LONG["messages"],
                workload.LONG["max_tokens"],
                on_content=first.set,
            )
            box["status"] = result["status"]
        except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            box["status"] = 0
        finally:
            done.set()

    thread = threading.Thread(target=long_job)
    thread.start()
    if not first.wait(5):
        done.wait(2)
        thread.join(5)
        fail("long scripted reply did not start streaming")
        return
    short = stream_chat(workload.SHORT["messages"], workload.SHORT["max_tokens"])
    still = not done.is_set()
    thread.join(10)
    if short["status"] == 200 and box.get("status") == 200 and still:
        say("Short scripted reply finished while the long scripted reply was still streaming.")
    else:
        fail("short scripted reply did not finish while the long scripted reply was still streaming")


def engine_ready_status():
    status, _ = exchange("GET", "/ready", timeout=2, port=8789)
    return status


def door_health_status():
    status, _ = exchange("GET", "/health", timeout=2, port=8788)
    return status


def run_cold():
    thread = None
    set_ready(0)
    box = {}

    def job():
        try:
            status, _body, _latency = buffered_chat(
                [{"role": "user", "content": "cold window request"}],
                16,
            )
            box["status"] = status
        except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            box["status"] = 0

    try:
        started = time.perf_counter()
        thread = threading.Thread(target=job)
        thread.start()
        state = wait_until(lambda item: item["peopleWaiting"] != "People waiting: nobody", 5)
        if state is None:
            fail("cold request did not queue")
            return None
        if state["readyReplicas"] != 0 or state["saturation"] != workload.SATURATION_QUIET:
            fail("saturation came on while ready replicas were below the ceiling")
        else:
            say("Queue below the replica ceiling left the saturation signal quiet.")
        if engine_ready_status() != 503:
            fail("fake engine was ready while replicas were zero")
        else:
            say("Fake engine was unready while the cold request waited.")
        if door_health_status() != 200:
            fail("door was down during the cold wait")
        set_ready(workload.REPLICA_CEILING)
        thread.join(10)
        elapsed = (time.perf_counter() - started) * 1000
        if box.get("status") != 200:
            fail("cold request did not succeed")
            return None
        return round(elapsed, 1)
    finally:
        set_ready(workload.REPLICA_CEILING)
        if thread is not None:
            thread.join(5)


def run_rejection():
    set_ready(workload.REPLICA_CEILING)
    before = get_json("/api/state")
    if before["saturation"] != workload.SATURATION_QUIET or before["peopleWaiting"] != "People waiting: nobody":
        fail("saturation was already on before the budget rejection")
        return
    status, _body = post_json("/v1/chat/completions", {
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "budget check tokens"}],
        "max_tokens": 32,
        "stream": False,
    }, credential="fixture-alpha")
    after = get_json("/api/state")
    quiet = (
        status == 429
        and after["saturation"] == workload.SATURATION_QUIET
        and after["peopleWaiting"] == "People waiting: nobody"
        and after["budgetRejections"] == before["budgetRejections"] + 1
    )
    if quiet:
        say("A 429 left the saturation signal quiet.")
    else:
        fail("a 429 changed the saturation signal")


def slots_full(state):
    return (
        state["readyReplicas"] == workload.REPLICA_CEILING
        and state["gpusIdle"] == "GPUs idle: none"
        and state["peopleWaiting"] == "People waiting: nobody"
    )


def ceiling_once():
    set_ready(workload.REPLICA_CEILING)
    threads = []

    def long_job():
        try:
            stream_chat(workload.LONG["messages"], workload.LONG["max_tokens"])
        except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            return

    for _index in range(workload.REPLICA_CEILING):
        thread = threading.Thread(target=long_job)
        thread.start()
        threads.append(thread)
    seen = None
    try:
        full = wait_until(slots_full, 5)
        if full is None:
            return None
        waiter = {}

        def wait_job():
            try:
                status, _body, _latency = buffered_chat(
                    [{"role": "user", "content": "queued fixture work"}],
                    16,
                )
                waiter["status"] = status
            except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
                waiter["status"] = 0

        hold = threading.Thread(target=wait_job)
        hold.start()
        threads.append(hold)
        seen = wait_until(
            lambda state: state["saturation"] == workload.SATURATION_FULL and state["peopleWaiting"] != "People waiting: nobody",
            5,
        )
        hold.join(10)
        if waiter.get("status") != 200:
            return None
        return seen
    finally:
        for thread in threads:
            thread.join(10)


def run_ceiling():
    seen = None
    for _attempt in range(2):
        seen = ceiling_once()
        if seen is not None:
            break
    if seen is None:
        fail("saturation stayed quiet at the replica ceiling with work waiting")
        return
    say(seen["saturation"])
    say(seen["peopleWaiting"])
    say("Ready replicas: %d" % seen["readyReplicas"])
    drained = wait_until(
        lambda state: state["saturation"] == workload.SATURATION_QUIET and state["peopleWaiting"] == "People waiting: nobody",
        5,
    )
    if drained is None:
        fail("saturation stayed on after the queue drained")


def reject_extra_fields():
    status, _body = post_json("/api/report", {
        "kind": "warm",
        "p50Ms": 1,
        "p95Ms": 1,
        "outputTokensPerSecond": 1,
        "clientTimeToFirstTokenMs": 1,
        "qualityMatched": 1,
        "result": "filled",
        "choice": "filled",
    })
    if status != 400:
        fail("a report with result and choice was stored")
        return
    state = get_json("/api/state")
    warm = state["reports"]["warm"]
    if not isinstance(warm, dict) or "result" in warm or "choice" in warm:
        fail("stored warm report picked up a result or a choice")


def check_formula():
    got = workload.cost_per_thousand(Decimal("2"), Decimal("400000"))
    line = workload.formula_check_line()
    dollars = (Decimal("1.000000") * workload.SPOT_NODE_PRICE + Decimal("1.000000") * workload.ON_DEMAND_NODE_PRICE).quantize(Decimal("0.000001"))
    expected = workload.cost_per_thousand(dollars, 400000).quantize(Decimal("0.000001"))
    sample = workload.describe_cost(3600, 400000)
    empty = workload.describe_cost(3600, 0)
    if got != Decimal("0.005") or "$0.005" not in line or not line.startswith("Formula check only."):
        fail("formula check")
        return False
    if sample["exact"] != format(expected, "f") or "Spot:" not in sample["text"] or "On-demand:" not in sample["text"]:
        fail("fixture cost did not use the same formula")
        return False
    if empty["value"] is not None:
        fail("cost ratio was defined with no tokens")
        return False
    say(line)
    return True


def check_live_cost(state):
    text = state.get("costDetail") or ""
    spot = re.search(r"Spot: ([0-9.]+) node hours × \$([0-9.]+) fixture = \$([0-9.]+)\.", text)
    demand = re.search(r"On-demand: ([0-9.]+) node hours × \$([0-9.]+) fixture = \$([0-9.]+)\.", text)
    total = re.search(r"Sum: \$([0-9.]+) / \((\d+) / 1000\) = \$([0-9.]+) per thousand\.", text)
    if not spot or not demand or not total:
        fail("cost detail did not keep spot and on-demand separate")
        return
    spot_hours = Decimal(spot.group(1))
    spot_price = Decimal(spot.group(2))
    spot_dollars = Decimal(spot.group(3))
    demand_hours = Decimal(demand.group(1))
    demand_price = Decimal(demand.group(2))
    demand_dollars = Decimal(demand.group(3))
    summed = Decimal(total.group(1))
    tokens = Decimal(total.group(2))
    shown = Decimal(total.group(3))
    again = workload.cost_per_thousand(summed, tokens)
    if again is None or again.quantize(Decimal("0.000001")) != shown:
        fail("live cost ratio did not match the formula")
        return
    if (spot_hours * spot_price).quantize(Decimal("0.000001")) != spot_dollars:
        fail("spot dollars did not match node hours times price")
        return
    if (demand_hours * demand_price).quantize(Decimal("0.000001")) != demand_dollars:
        fail("on-demand dollars did not match node hours times price")
        return
    if spot_hours != demand_hours or summed != (spot_dollars + demand_dollars).quantize(Decimal("0.000001")):
        fail("spot and on-demand hours were not both in the sum")
        return
    if "not a cloud price" not in text:
        fail("fixture cost was labeled as a cloud price")
        return
    say("Live cost recomputes from fixture node hours, fixture price, and tokens served.")


def check_page():
    status, html = exchange("GET", "/", timeout=5)
    if status != 200:
        fail("fleet page did not load")
        return
    text = html.decode("utf-8", errors="replace")
    needles = (
        "job finished",
        "weights published",
        "replica ready",
        "pending pods 1. No node yet.",
        "node appeared. Spot, fixture. Pending pods 0.",
        'id="note-warm-result" class="note-value"></span>',
        'id="note-warm-choice" class="note-value"></span>',
        'id="note-spot-result" class="note-value"></span>',
        'id="note-spot-choice" class="note-value"></span>',
        "Cold-start time until first success",
        "Share of GPU hours on spot",
        "Formula check only. $2 of GPU and 400,000 tokens is $0.005 per thousand. Not the fleet ratio.",
        "Outside the headline: control plane, CPU door, local edge, weight storage.",
        "GPUs idle:",
        "People waiting:",
    )
    missing = [needle for needle in needles if needle not in text]
    if missing:
        fail("fleet page is missing timeline, notes, or the formula check")


SERVING_PHRASES = (
    "time to first token",
    "inter-token latency",
    "end-to-end latency",
    "queue wait",
    "kv cache",
    "running",
    "waiting",
    "tokens per second",
)
SERVING_BANNED = ("per thousand", "node hours", "formula check")
ENGINE_METRICS = (
    "vllm:time_to_first_token_seconds_count",
    "vllm:time_per_output_token_seconds_count",
    "vllm:e2e_request_latency_seconds_count",
    "vllm:request_queue_time_seconds_count",
    "vllm:request_prefill_time_seconds_count",
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:gpu_cache_usage_perc",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:num_preemptions_total",
)


def parse_metrics(text):
    found = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, value = line.rsplit(" ", 1)
        found[name] = float(value)
    return found


def scrape(port):
    status, body = exchange("GET", "/metrics", timeout=5, port=port)
    if status != 200:
        raise RuntimeError("metrics %s returned %s" % (port, status))
    return parse_metrics(body.decode("utf-8"))


def set_hold(seconds):
    status, body = post_json("/api/hold-budget", {"seconds": seconds})
    if status != 200:
        raise RuntimeError("hold")
    return json.loads(body.decode("utf-8"))["seconds"]


def get_remaining(credential):
    status, body = exchange(
        "GET",
        "/api/budget",
        headers={"Authorization": "Bearer " + credential},
        timeout=5,
    )
    if status != 200:
        raise RuntimeError("budget")
    payload = json.loads(body.decode("utf-8"))
    return payload["remaining"], payload["reserved"]


def check_serving_page():
    status, html = exchange("GET", "/serving", timeout=5)
    if status != 200:
        fail("serving page did not load")
        return
    text = html.decode("utf-8", errors="replace").lower()
    missing = [phrase for phrase in SERVING_PHRASES if phrase not in text]
    banned = [phrase for phrase in SERVING_BANNED if phrase in text]
    if missing or banned:
        fail("serving page is missing latency panels or includes a cost ratio")


def check_serving_value():
    status, body = exchange("GET", "/api/serving", timeout=5)
    if status != 200:
        fail("serving metrics did not load")
        return
    text = body.decode("utf-8", errors="replace")
    lowered = text.lower()
    if any(phrase in lowered for phrase in SERVING_BANNED):
        fail("serving metrics include a cost ratio")
        return
    payload = json.loads(text)
    ttft = (payload.get("timeToFirstToken") or {}).get("p50")
    status_text = (payload.get("timeToFirstToken") or {}).get("status")
    if isinstance(ttft, bool) or not isinstance(ttft, (int, float)) or status_text == "no finished requests":
        fail("serving page kept the empty time to first token state")
        return
    say("Serving page has a time to first token after the warm card.")


def run_hold_timeout():
    set_ready(0)
    try:
        set_hold(0.4)
        before_remaining, before_reserved = get_remaining("fixture-beta")
        status, _body, _latency = buffered_chat(
            [{"role": "user", "content": "hold check"}],
            16,
        )
        state = get_json("/api/state")
        after_remaining, after_reserved = get_remaining("fixture-beta")
        quiet = (
            status == 503
            and state["readyReplicas"] == 0
            and state["peopleWaiting"] == "People waiting: nobody"
            and state["saturation"] == workload.SATURATION_QUIET
            and after_remaining == before_remaining
            and after_reserved == before_reserved
            and door_health_status() == 200
            and engine_ready_status() == 503
        )
        if quiet:
            say("A hold that outlived the budget returned 503 and released the reserve.")
        else:
            fail("a short hold did not return 503 with the reserve released")
    except (OSError, json.JSONDecodeError, KeyError, RuntimeError, TypeError, ValueError):
        fail("a short hold did not return 503 with the reserve released")
    finally:
        try:
            set_hold(30)
        except (OSError, json.JSONDecodeError, RuntimeError, TypeError, ValueError):
            fail("could not restore the hold budget")
        set_ready(workload.REPLICA_CEILING)


def _metric_is(found, name, expected):
    value = found.get(name)
    return isinstance(value, float) and value == float(expected)


def run_metrics():
    set_ready(workload.REPLICA_CEILING)
    try:
        before = scrape(8789)
        result = stream_chat([{"role": "user", "content": "metrics sample"}], 4)
        after = scrape(8789)
    except (OSError, json.JSONDecodeError, RuntimeError, TypeError, ValueError):
        fail("engine metrics did not load")
        return
    before_count = before.get("vllm:time_to_first_token_seconds_count")
    after_count = after.get("vllm:time_to_first_token_seconds_count")
    names_ok = all(name in after for name in ENGINE_METRICS)
    if result["status"] == 200 and before_count is not None and after_count is not None and after_count >= before_count + 1 and names_ok:
        say("A streamed completion increased the time to first token histogram.")
    else:
        fail("streamed completion did not increase the time to first token histogram")
    try:
        door_before = scrape(8788)
        status, _body = post_json("/v1/chat/completions", {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "metrics sample"}],
            "max_tokens": 8,
            "stream": False,
        })
        door_after = scrape(8788)
    except (OSError, json.JSONDecodeError, RuntimeError, TypeError, ValueError):
        fail("door metrics did not load")
        return
    auth_key = 'door_requests_total{code="401"}'
    if (
        status == 401
        and _metric_is(door_after, auth_key, door_before.get(auth_key, 0) + 1)
        and door_after.get("door_budget_rejections_total") == door_before.get("door_budget_rejections_total")
        and door_after.get("door_requests_waiting") == door_before.get("door_requests_waiting")
    ):
        say("A 401 increased only the 401 counter.")
    else:
        fail("a 401 changed rejections or waiting, or missed the counter")
    try:
        door_before = scrape(8788)
        status, _body = post_json("/v1/chat/completions", {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "metrics sample"}],
            "max_tokens": 32,
            "stream": False,
        }, credential="fixture-alpha")
        door_after = scrape(8788)
    except (OSError, json.JSONDecodeError, RuntimeError, TypeError, ValueError):
        fail("door metrics did not load for the budget rejection")
        return
    if (
        status == 429
        and door_before.get("door_requests_waiting") == 0
        and _metric_is(door_after, "door_budget_rejections_total", door_before.get("door_budget_rejections_total", 0) + 1)
        and door_after.get("door_requests_waiting") == 0
    ):
        say("A 429 increased rejections and left waiting at 0.")
    else:
        fail("a 429 changed waiting or missed the rejection counter")
    set_ready(0)
    box = {}

    def job():
        try:
            status, _body, _latency = buffered_chat(
                [{"role": "user", "content": "metrics sample"}],
                8,
            )
            box["status"] = status
        except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            box["status"] = 0

    thread = threading.Thread(target=job)
    thread.start()
    try:
        seen = wait_until(lambda item: item["peopleWaiting"] != "People waiting: nobody", 5)
        door = scrape(8788)
        engine = scrape(8789)
        waiting = door.get("door_requests_waiting")
        in_flight = door.get("door_requests_in_flight")
        running = engine.get("vllm:num_requests_running")
        engine_waiting = engine.get("vllm:num_requests_waiting")
        series = (waiting or 0) + (in_flight or 0) + (running or 0) + (engine_waiting or 0)
        held = (
            seen is not None
            and waiting == 1
            and in_flight == 0
            and running == 0
            and engine_waiting == 0
            and series == 1
        )
        if held:
            say("A held request showed waiting 1 and in-flight 0.")
        else:
            fail("a held request did not show waiting 1 and in-flight 0")
    except (OSError, json.JSONDecodeError, RuntimeError, TypeError, ValueError):
        fail("a held request did not show waiting 1 and in-flight 0")
    finally:
        set_ready(workload.REPLICA_CEILING)
        thread.join(10)
    if box.get("status") != 200:
        fail("held request did not finish after a replica was ready")
        return
    quiet = wait_until(
        lambda item: item["peopleWaiting"] == "People waiting: nobody" and item["gpusIdle"] != "GPUs idle: none",
        5,
    )
    try:
        door = scrape(8788)
        engine = scrape(8789)
        state = get_json("/api/state")
    except (OSError, json.JSONDecodeError, RuntimeError, TypeError, ValueError):
        fail("busy series did not match the door and the engine")
        return
    waiting = state["fleet"]["waitingRequests"]["value"]
    ready = state["fleet"]["readyGpus"]["value"]
    idle = state["fleet"]["idleGpus"]["value"]
    in_flight = ready - idle
    running = engine.get("vllm:num_requests_running")
    engine_waiting = engine.get("vllm:num_requests_waiting")
    series = door.get("door_requests_waiting", 0) + door.get("door_requests_in_flight", 0) + (running or 0) + (engine_waiting or 0)
    parts = waiting + in_flight + (running or 0) + (engine_waiting or 0)
    matched = (
        quiet is not None
        and door.get("door_requests_waiting") == waiting
        and door.get("door_requests_in_flight") == in_flight
        and series == parts
    )
    if matched:
        say("Busy series match waiting, in flight, and the engine.")
    else:
        fail("busy series did not match the door and the engine")


def scan_for_prompts():
    leaks = (
        "named-line",
        "First line named-line",
        "Keys named alpha beta",
        "short fixture script",
        "long fixture script",
        "tok tok",
        "fixture-beta",
        "fixture-alpha",
        "Bearer ",
        "cold window request",
        "queued fixture work",
        "budget check tokens",
        "hold check",
        "metrics sample",
    )
    blobs = {
        "state": json.dumps(get_json("/api/state")),
        "events": json.dumps(get_json("/api/events")),
        "traces": json.dumps(get_json("/api/traces")),
    }
    for path in ("/", "/app.js", "/styles.css", "/serving", "/serving.js"):
        status, body = exchange("GET", path, timeout=5)
        if status == 200:
            blobs[path] = body.decode("utf-8", errors="replace")
    for port, name in ((8788, "door metrics"), (8789, "engine metrics")):
        status, body = exchange("GET", "/metrics", timeout=5, port=port)
        if status != 200:
            fail(name + " did not load")
            continue
        blobs[name] = body.decode("utf-8", errors="replace")
    for name in ("warm.txt", "cold.txt", "checks.txt"):
        file_path = REPORTS / name
        if file_path.exists():
            blobs[name] = file_path.read_text(encoding="utf-8")
    found = [name for name, text in blobs.items() if any(leak in text for leak in leaks)]
    if found:
        fail("frozen prompt text showed up in " + ", ".join(found))
    else:
        say("Frozen prompt text stayed off the state, the events, the traces, and the page.")


def write_report(name, text):
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / name).write_text(text, encoding="utf-8")


def main():
    if not check_formula():
        return 1
    if not edge_up():
        say("The local edge is not up. From this folder, start it with python3 serve.py")
        say("Then run python3 harness.py")
        return 1
    check_page()
    check_serving_page()
    warm = run_warm()
    if warm is not None:
        warm_text = format_warm(warm)
        write_report("warm.txt", warm_text)
        say(warm_text.rstrip("\n"))
        post_report({"kind": "warm", **warm})
    run_overlap()
    cold_ms = run_cold()
    if cold_ms is not None:
        cold_text = format_cold(cold_ms)
        write_report("cold.txt", cold_text)
        say(cold_text.rstrip("\n"))
        post_report({"kind": "cold", "timeUntilFirstSuccessMs": cold_ms})
        if "p50" in cold_text:
            fail("cold report included a warm percentile")
    run_hold_timeout()
    run_rejection()
    run_ceiling()
    run_metrics()
    check_serving_value()
    if warm is not None:
        reject_extra_fields()
    state = get_json("/api/state")
    if state.get("formulaCheck") != workload.formula_check_line():
        fail("formula line on the state does not match the formula check")
    reports = state.get("reports") or {}
    if warm is not None and isinstance(reports.get("warm"), dict):
        if "timeUntilFirstSuccessMs" in reports["warm"] or reports["warm"].get("qualityMatched") != warm["qualityMatched"]:
            fail("warm report on the state did not keep the pass count separate from cold time")
    if cold_ms is not None and isinstance(reports.get("cold"), dict):
        cold_seen = reports["cold"].get("timeUntilFirstSuccessMs")
        if "p50Ms" in reports["cold"] or not isinstance(cold_seen, (int, float)) or abs(cold_seen - cold_ms) > 0.001:
            fail("cold report on the state included a warm percentile")
    if state.get("tokensServed", 0) > 0:
        check_live_cost(state)
    else:
        fail("no tokens were served")
    observations = [
        workload.formula_check_line(),
        "Warm report and cold report are separate.",
        "Result and choice stay empty on the decision notes.",
    ]
    write_report("checks.txt", "\n".join(observations) + "\n")
    scan_for_prompts()
    if FAILURES:
        return 1
    say("Reports: %s" % REPORTS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
