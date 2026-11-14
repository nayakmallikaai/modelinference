"""Shared fixture telemetry for the local edge, door, and fake engine.

Prompt text is never stored. Caller identity stays in the door budget table
and is not copied onto events, traces, or gauges.
"""

import os
import threading
import time

import tokens
import workload

EVENT_TYPES = {
    "request admitted",
    "first token",
    "token",
    "finish",
    "scale decision",
    "node launch",
    "spot reclaim",
}

PUBLIC_EVENT_KEYS = (
    "time",
    "type",
    "traceId",
    "replica",
    "node",
    "capacityType",
    "readyReplicas",
    "index",
    "promptTokens",
    "completionTokens",
    "totalTokens",
    "timeToFirstTokenMs",
    "interTokenLatencyMs",
)

SPAN_NAMES = ("local edge", "door", "fake engine")

# Test doubles so a 429 can tell one caller from another. Not an auth decision.
FIXTURE_BUDGETS = {
    "fixture-alpha": 8,
    "fixture-beta": 100000,
}

DEFAULT_MAX_TOKENS = 16
MAX_TOKENS = 8192
CAPACITY = workload.REPLICA_CEILING
SPOT_REPLICAS = 3
DEFAULT_HOLD_SECONDS = 30.0
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
HISTOGRAMS = (
    "vllm:time_to_first_token_seconds",
    "vllm:time_per_output_token_seconds",
    "vllm:e2e_request_latency_seconds",
    "vllm:request_queue_time_seconds",
    "vllm:request_prefill_time_seconds",
)
STATUS_CODES = (200, 401, 429, 503)


class Stopping(Exception):
    pass


class HoldExpired(Exception):
    pass


class Observation:
    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.hits = [0 for _bound in BUCKETS]
        self.samples = []

    def observe(self, value):
        value = float(value)
        if value < 0:
            value = 0.0
        self.count += 1
        self.total += value
        for index, bound in enumerate(BUCKETS):
            if value <= bound:
                self.hits[index] += 1
        self.samples.append(value)
        if len(self.samples) > 4096:
            del self.samples[0]

    def percentile(self, pct):
        ordered = sorted(self.samples)
        if not ordered:
            return None
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * (pct / 100.0)
        low = int(rank)
        high = min(low + 1, len(ordered) - 1)
        weight = rank - low
        return ordered[low] * (1 - weight) + ordered[high] * weight


def _bound_label(bound):
    text = "%.3f" % bound
    return text.rstrip("0").rstrip(".")


def hold_budget_default():
    raw = os.environ.get("HOLD_BUDGET_SECONDS")
    if raw is None or raw.strip() == "":
        return DEFAULT_HOLD_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_HOLD_SECONDS
    if value < 0 or value > 3600:
        return DEFAULT_HOLD_SECONDS
    return value


def count_text_tokens(text):
    return tokens.count_text_tokens(text)


def count_messages(payload):
    total = 0
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return 0
    for message in messages:
        if isinstance(message, dict):
            total += _count_content(message.get("content"))
    return total


def _count_content(content):
    if isinstance(content, str):
        return count_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                total += count_text_tokens(part.get("text"))
        return total
    return 0


def _report_number(payload, key):
    if key not in payload:
        raise ValueError(key)
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(key)
    if value < 0 or value > 1_000_000_000:
        raise ValueError(key)
    return round(float(value), 3)


def _report_count(payload, key):
    if key not in payload:
        raise ValueError(key)
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(key)
    if value < 0 or value > 1000:
        raise ValueError(key)
    return value


_WARM_FIELDS = {
    "kind",
    "p50Ms",
    "p95Ms",
    "outputTokensPerSecond",
    "clientTimeToFirstTokenMs",
    "qualityMatched",
}
_COLD_FIELDS = {"kind", "timeUntilFirstSuccessMs"}


def parse_max_tokens(payload):
    if not isinstance(payload, dict) or "max_tokens" not in payload or payload.get("max_tokens") is None:
        return DEFAULT_MAX_TOKENS, None
    value = payload.get("max_tokens")
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "max_tokens must be an integer"
    if value < 1 or value > MAX_TOKENS:
        return None, "max_tokens is outside the fixture range"
    return value, None


class FleetState:
    def __init__(self):
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.origin = time.perf_counter()
        self.ready_replicas = CAPACITY
        self.in_flight = 0
        self.waiting = 0
        self.stopping = False
        self.kv_fill = 0.02
        self.budget_rejections = 0
        self.hold_budget = hold_budget_default()
        self.engine_running = 0
        self.engine_waiting = 0
        self.prompt_tokens_total = 0
        self.generation_tokens_total = 0
        self.preemptions_total = 0
        self.statuses = {code: 0 for code in STATUS_CODES}
        self.histograms = {name: Observation() for name in HISTOGRAMS}
        self.budgets = {
            name: {"remaining": amount, "reserved": 0}
            for name, amount in FIXTURE_BUDGETS.items()
        }
        self.events = []
        self.traces = {}
        self.sampled_trace_id = None
        self.reports = {"warm": None, "cold": None}
        self._launch_and_scale()

    def now_ms(self):
        return (time.perf_counter() - self.origin) * 1000

    def _stamp(self):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _emit(self, event):
        kind = event.get("type")
        if kind not in EVENT_TYPES:
            return
        stored = dict(event)
        stored["time"] = self._stamp()
        stored["_at"] = time.perf_counter()
        self.events.append(stored)
        trace_id = stored.get("traceId")
        trace = self.traces.get(trace_id) if trace_id else None
        if trace is not None:
            trace["events"].append(stored)

    def _launch_and_scale(self):
        with self.cv:
            self._emit({
                "type": "node launch",
                "node": "node-spot-1",
                "capacityType": "spot",
                "readyReplicas": SPOT_REPLICAS,
            })
            self._emit({
                "type": "node launch",
                "node": "node-ondemand-1",
                "capacityType": "on-demand",
                "readyReplicas": 1,
            })
            self._emit_scale_locked(CAPACITY)

    def _replica_node(self, index):
        if index < SPOT_REPLICAS:
            return "node-spot-1"
        return "node-ondemand-1"

    def _emit_scale_locked(self, ready):
        for index in range(CAPACITY):
            self._emit({
                "type": "scale decision",
                "replica": "replica-%d" % (index + 1),
                "node": self._replica_node(index),
                "readyReplicas": ready,
            })

    def set_ready(self, ready):
        ready = max(0, min(CAPACITY, int(ready)))
        with self.cv:
            self.ready_replicas = ready
            self._emit_scale_locked(ready)
            self.cv.notify_all()
        return ready

    def set_hold_budget(self, seconds):
        seconds = float(seconds)
        if seconds < 0 or seconds > 3600:
            raise ValueError("hold")
        with self.cv:
            self.hold_budget = seconds
            self.cv.notify_all()
            return self.hold_budget

    def note_status(self, code):
        try:
            code = int(code)
        except (TypeError, ValueError):
            return
        with self.cv:
            self.statuses[code] = self.statuses.get(code, 0) + 1

    def engine_enter(self):
        with self.cv:
            self.engine_running += 1

    def engine_leave(self):
        with self.cv:
            if self.engine_running > 0:
                self.engine_running -= 1

    def budget_remaining(self, caller):
        with self.cv:
            slot = self.budgets.get(caller)
            if slot is None:
                return None
            return slot["remaining"], slot["reserved"]

    def spot_reclaim(self):
        with self.cv:
            spot_ready = min(SPOT_REPLICAS, self.ready_replicas)
            self.ready_replicas = max(0, self.ready_replicas - spot_ready)
            self._emit({
                "type": "spot reclaim",
                "node": "node-spot-1",
                "readyReplicas": self.ready_replicas,
            })
            self._emit_scale_locked(self.ready_replicas)
            self.cv.notify_all()
            return self.ready_replicas

    def close(self):
        with self.cv:
            self.stopping = True
            self.cv.notify_all()

    def authorize(self, header):
        if not isinstance(header, str) or not header.strip():
            return None, "missing"
        text = header.strip()
        if not text.lower().startswith("bearer "):
            return None, "missing"
        token = text[7:].strip()
        if not token:
            return None, "missing"
        if token not in self.budgets:
            return None, "unknown"
        return token, None

    def try_reserve(self, caller, estimate):
        with self.cv:
            slot = self.budgets[caller]
            if slot["remaining"] < estimate:
                self.budget_rejections += 1
                return False
            slot["remaining"] -= estimate
            slot["reserved"] += estimate
            return True

    def settle(self, caller, estimate, actual):
        actual = max(0, int(actual))
        with self.cv:
            slot = self.budgets[caller]
            slot["reserved"] = max(0, slot["reserved"] - estimate)
            slot["remaining"] += estimate - actual
            if slot["remaining"] < 0:
                slot["remaining"] = 0

    def release(self, caller, estimate):
        self.settle(caller, estimate, 0)

    def begin_trace(self, trace_id):
        with self.cv:
            self.traces[trace_id] = {
                "traceId": trace_id,
                "sampled": False,
                "spans": [],
                "events": [],
            }

    def end_span(self, trace_id, name, start_ms, end_ms):
        if name not in SPAN_NAMES:
            return
        with self.cv:
            trace = self.traces.get(trace_id)
            if trace is None:
                return
            trace["spans"].append({
                "name": name,
                "startMs": round(start_ms, 1),
                "endMs": round(end_ms, 1),
            })

    def take_sample(self, trace_id):
        with self.cv:
            trace = self.traces.get(trace_id)
            if trace is None or self.sampled_trace_id is not None:
                return False
            self.sampled_trace_id = trace_id
            trace["sampled"] = True
            return True

    def begin_wait(self):
        with self.cv:
            self.waiting += 1
            started = time.perf_counter()
            try:
                while self.ready_replicas <= 0 or self.in_flight >= self.ready_replicas:
                    if self.stopping:
                        raise Stopping()
                    elapsed = time.perf_counter() - started
                    if elapsed >= self.hold_budget:
                        raise HoldExpired()
                    remaining = self.hold_budget - elapsed
                    self.cv.wait(timeout=min(0.5, max(0.0, remaining)))
            except BaseException:
                self.waiting -= 1
                raise
            self.waiting -= 1
            self.in_flight += 1
            queue_ms = round((time.perf_counter() - started) * 1000, 1)
            replica_index = min(CAPACITY, max(1, self.in_flight)) - 1
            if self.ready_replicas <= replica_index:
                replica_index = self.ready_replicas - 1
            replica = "replica-%d" % (replica_index + 1)
            self.kv_fill = min(0.95, 0.02 + 0.15 * self.in_flight)
            return replica, queue_ms

    def begin_wait_remote(self, ready):
        """Hold while a remote engine is down. `ready` is called without the lock."""
        with self.cv:
            self.waiting += 1
            started = time.perf_counter()
        try:
            while True:
                if ready():
                    break
                with self.cv:
                    if self.stopping:
                        raise Stopping()
                    elapsed = time.perf_counter() - started
                    if elapsed >= self.hold_budget:
                        raise HoldExpired()
                time.sleep(0.25)
        except BaseException:
            with self.cv:
                if self.waiting > 0:
                    self.waiting -= 1
                self.cv.notify_all()
            raise
        with self.cv:
            if self.waiting > 0:
                self.waiting -= 1
            self.in_flight += 1
            queue_ms = round((time.perf_counter() - started) * 1000, 1)
            self.kv_fill = min(0.95, 0.02 + 0.15 * self.in_flight)
            self.cv.notify_all()
            return "replica-1", queue_ms

    def end_flight(self):
        with self.cv:
            if self.in_flight > 0:
                self.in_flight -= 1
            self.kv_fill = min(0.95, 0.02 + 0.15 * self.in_flight)
            self.cv.notify_all()

    def admit(self, trace_id, replica, queue_ms=0):
        with self.cv:
            trace = self.traces.get(trace_id)
            if trace is not None:
                trace["queueMs"] = queue_ms
            self._emit({
                "type": "request admitted",
                "traceId": trace_id,
                "replica": replica,
            })

    def first_token(self, trace_id, ttft_ms):
        with self.cv:
            self._emit({
                "type": "first token",
                "traceId": trace_id,
                "timeToFirstTokenMs": ttft_ms,
            })

    def token(self, trace_id, index):
        with self.cv:
            self._emit({
                "type": "token",
                "traceId": trace_id,
                "index": index,
            })

    def finish(self, trace_id, replica, prompt_tokens, completion_tokens, ttft_ms, itl_ms, e2e_ms):
        total = prompt_tokens + completion_tokens
        event = {
            "type": "finish",
            "traceId": trace_id,
            "replica": replica,
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
            "totalTokens": total,
            "timeToFirstTokenMs": ttft_ms,
            "interTokenLatencyMs": itl_ms,
        }
        with self.cv:
            self._emit(event)
            self.prompt_tokens_total += prompt_tokens
            self.generation_tokens_total += completion_tokens
            self.histograms["vllm:time_to_first_token_seconds"].observe((ttft_ms or 0) / 1000.0)
            self.histograms["vllm:request_prefill_time_seconds"].observe((ttft_ms or 0) / 1000.0)
            if itl_ms is not None:
                self.histograms["vllm:time_per_output_token_seconds"].observe(itl_ms / 1000.0)
            self.histograms["vllm:e2e_request_latency_seconds"].observe((e2e_ms or 0) / 1000.0)
            queue_ms = 0
            trace = self.traces.get(trace_id)
            if trace is not None and isinstance(trace.get("queueMs"), (int, float)) and not isinstance(trace.get("queueMs"), bool):
                queue_ms = trace["queueMs"]
            self.histograms["vllm:request_queue_time_seconds"].observe(queue_ms / 1000.0)

    def public_event(self, event):
        out = {}
        for key in PUBLIC_EVENT_KEYS:
            if key not in event:
                continue
            value = event[key]
            if isinstance(value, str) and len(value) > 80:
                continue
            out[key] = value
        return out

    def public_events(self):
        with self.cv:
            return [self.public_event(event) for event in self.events]

    def public_traces(self):
        with self.cv:
            traces = []
            for trace in self.traces.values():
                spans = sorted(trace["spans"], key=lambda span: span["startMs"])
                traces.append({
                    "traceId": trace["traceId"],
                    "sampled": trace["sampled"],
                    "spans": spans,
                    "events": [self.public_event(event) for event in trace["events"]],
                })
            return traces

    def public_trace(self, trace_id):
        for trace in self.public_traces():
            if trace["traceId"] == trace_id:
                return trace
        return None

    def store_report(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("report")
        kind = payload.get("kind")
        if kind == "warm":
            allowed = _WARM_FIELDS
        elif kind == "cold":
            allowed = _COLD_FIELDS
        else:
            raise ValueError("kind")
        if set(payload) - allowed:
            raise ValueError("extra")
        card = {
            "kind": kind,
            "concurrency": workload.CONCURRENCY,
            "promptLengths": list(workload.PROMPT_LENGTHS),
            "outputLengths": list(workload.OUTPUT_LENGTHS),
            "streaming": bool(workload.STREAMING),
        }
        if kind == "warm":
            card["p50Ms"] = _report_number(payload, "p50Ms")
            card["p95Ms"] = _report_number(payload, "p95Ms")
            card["outputTokensPerSecond"] = _report_number(payload, "outputTokensPerSecond")
            card["clientTimeToFirstTokenMs"] = _report_number(payload, "clientTimeToFirstTokenMs")
            card["qualityMatched"] = _report_count(payload, "qualityMatched")
        else:
            card["timeUntilFirstSuccessMs"] = _report_number(payload, "timeUntilFirstSuccessMs")
        with self.cv:
            self.reports[kind] = card
            return dict(card)

    def snapshot(self):
        with self.cv:
            ready = self.ready_replicas
            idle = max(0, ready - self.in_flight)
            waiting = self.waiting
            kv = self.kv_fill
            rejections = self.budget_rejections
            finishes = [event for event in self.events if event.get("type") == "finish"]
            origin = self.origin
            reports = {
                "warm": None if self.reports["warm"] is None else dict(self.reports["warm"]),
                "cold": None if self.reports["cold"] is None else dict(self.reports["cold"]),
            }
            now = time.perf_counter()
        tokens_served = sum(event.get("totalTokens") or 0 for event in finishes)
        recent = [event for event in finishes if now - event.get("_at", now) <= 60]
        completion = sum(event.get("completionTokens") or 0 for event in recent)
        tokens_per_second = completion / 60
        ttfts = [event["timeToFirstTokenMs"] for event in finishes if event.get("timeToFirstTokenMs") is not None]
        itls = [event["interTokenLatencyMs"] for event in finishes if event.get("interTokenLatencyMs") is not None]
        if idle:
            idle_sentence = "GPUs idle: %d" % idle
        else:
            idle_sentence = "GPUs idle: none"
        if waiting:
            waiting_sentence = "People waiting: %d" % waiting
        else:
            waiting_sentence = "People waiting: nobody"
        spot = min(SPOT_REPLICAS, ready)
        on_demand = max(0, ready - SPOT_REPLICAS)
        if ready:
            ready_status = "ready"
        else:
            ready_status = "none ready"
        if idle == 0:
            idle_status = "none idle"
        elif idle == ready:
            idle_status = "all idle"
        else:
            idle_status = "some busy"
        if waiting:
            waiting_status = "people waiting"
        else:
            waiting_status = "nobody waiting"
        if tokens_per_second:
            rate_status = "from finish events"
        else:
            rate_status = "no traffic"
        if ttfts:
            ttft_value = round(sum(ttfts) / len(ttfts), 1)
            ttft_status = "fleet series"
        else:
            ttft_value = None
            ttft_status = "no finished requests"
        if itls:
            itl_value = round(sum(itls) / len(itls), 1)
            itl_status = "fleet series"
        else:
            itl_value = None
            itl_status = "no finished requests"
        elapsed = round(now - origin, 3)
        cost = workload.describe_cost(elapsed, tokens_served)
        saturation = workload.saturation_text(ready, waiting, CAPACITY)
        return {
            "gpusIdle": idle_sentence,
            "peopleWaiting": waiting_sentence,
            "fixture": "live",
            "fixtureLabel": "Live local signals",
            "notice": "Fixture signals on this computer. Not a cloud fleet.",
            "readyReplicas": ready,
            "tokensServed": tokens_served,
            "budgetRejections": rejections,
            "saturation": saturation,
            "costDetail": cost["text"],
            "formulaCheck": workload.formula_check_line(),
            "reports": reports,
            "fleet": {
                "readyGpus": {"value": ready, "status": ready_status},
                "idleGpus": {"value": idle, "status": idle_status},
                "waitingRequests": {"value": waiting, "status": waiting_status},
                "pendingPods": {"value": 0, "status": "none pending"},
                "spotGpus": {"value": spot, "status": "spot, fixture"},
                "onDemandGpus": {"value": on_demand, "status": "on-demand, fixture"},
            },
            "efficiency": {
                "tokensPerSecond": {"value": round(tokens_per_second, 2), "status": rate_status},
                "costPerThousandTokens": {"value": cost["value"], "status": cost["status"]},
                "kvFill": {"value": round(kv, 2), "status": "fixture gauge"},
            },
            "behavior": {
                "timeToFirstToken": {"value": ttft_value, "status": ttft_status},
                "interTokenLatency": {"value": itl_value, "status": itl_status},
            },
        }

    def serving(self):
        with self.cv:
            waiting = self.waiting
            running = self.engine_running
            kv = self.kv_fill
            statuses = {code: self.statuses.get(code, 0) for code in STATUS_CODES}
            series = {}
            for name in HISTOGRAMS:
                hist = self.histograms[name]
                if hist.count == 0 or not hist.samples:
                    series[name] = {"p50": None, "p95": None, "status": "no finished requests"}
                else:
                    series[name] = {
                        "p50": round(hist.percentile(50) * 1000, 1),
                        "p95": round(hist.percentile(95) * 1000, 1),
                        "status": "engine series",
                    }
            finishes = [event for event in self.events if event.get("type") == "finish"]
            now = time.perf_counter()
        recent = [event for event in finishes if now - event.get("_at", now) <= 60]
        completion = sum(event.get("completionTokens") or 0 for event in recent)
        if completion:
            rate_status = "from finish events"
        else:
            rate_status = "no traffic"
        return {
            "timeToFirstToken": series["vllm:time_to_first_token_seconds"],
            "interTokenLatency": series["vllm:time_per_output_token_seconds"],
            "endToEnd": series["vllm:e2e_request_latency_seconds"],
            "queueWait": series["vllm:request_queue_time_seconds"],
            "prefill": series["vllm:request_prefill_time_seconds"],
            "kvCache": {"value": round(kv, 2), "status": "engine gauge"},
            "running": running,
            "waiting": waiting,
            "tokensPerSecond": {"value": round(completion / 60, 2), "status": rate_status},
            "outcomes": {str(code): statuses[code] for code in STATUS_CODES},
        }

    def door_metrics(self):
        with self.cv:
            waiting = self.waiting
            in_flight = self.in_flight
            rejections = self.budget_rejections
            statuses = dict(self.statuses)
        lines = [
            "# TYPE door_requests_waiting gauge",
            "door_requests_waiting %d" % waiting,
            "# TYPE door_requests_in_flight gauge",
            "door_requests_in_flight %d" % in_flight,
            "# TYPE door_budget_rejections_total counter",
            "door_budget_rejections_total %d" % rejections,
            "# TYPE door_requests_total counter",
        ]
        for code in sorted(statuses):
            lines.append('door_requests_total{code="%d"} %d' % (code, statuses[code]))
        return "\n".join(lines) + "\n"

    def engine_metrics(self):
        with self.cv:
            running = self.engine_running
            waiting = self.engine_waiting
            kv = self.kv_fill
            prompt = self.prompt_tokens_total
            generated = self.generation_tokens_total
            preemptions = self.preemptions_total
            hists = {
                name: (hist.count, hist.total, list(hist.hits))
                for name, hist in self.histograms.items()
            }
        lines = [
            "# TYPE vllm:num_requests_running gauge",
            "vllm:num_requests_running %d" % running,
            "# TYPE vllm:num_requests_waiting gauge",
            "vllm:num_requests_waiting %d" % waiting,
            "# TYPE vllm:gpu_cache_usage_perc gauge",
            "vllm:gpu_cache_usage_perc %.6f" % kv,
            "# TYPE vllm:prompt_tokens_total counter",
            "vllm:prompt_tokens_total %d" % prompt,
            "# TYPE vllm:generation_tokens_total counter",
            "vllm:generation_tokens_total %d" % generated,
            "# TYPE vllm:num_preemptions_total counter",
            "vllm:num_preemptions_total %d" % preemptions,
        ]
        for name in HISTOGRAMS:
            count, total, hits = hists[name]
            lines.append("# TYPE %s histogram" % name)
            for bound, hit in zip(BUCKETS, hits):
                lines.append('%s_bucket{le="%s"} %d' % (name, _bound_label(bound), hit))
            lines.append('%s_bucket{le="+Inf"} %d' % (name, count))
            lines.append("%s_sum %.6f" % (name, total))
            lines.append("%s_count %d" % (name, count))
        return "\n".join(lines) + "\n"


STATE = FleetState()
