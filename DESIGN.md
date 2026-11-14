# LLM inference serving platform

Llama 3.1 8B Instruct, served by vLLM, behind an OpenAI-compatible API. The
laptop stack in this repo is the stand-in for the ALB, the door, and the
engine. AWS is a short, scripted window at the end.

The constraint that decides the shape: a GPU node-hour is the expensive thing.
Everything that can be proven without a GPU is proven here. The cluster exists
for one cold start, one frozen workload card, and a scale-to-zero, and then
the node and the cluster come down the same day.

## What already runs locally

`python3 serve.py` starts three processes. `python3 harness.py` is the contract.

| Local piece | Port | Production role |
| --- | --- | --- |
| `local_edge.py` | 8787 | ALB plus the fleet page. Proxies `/v1/chat/completions`, serves `/api/state`. |
| `door.py` | 8788 | Auth, per-client token budget, admission, streaming relay. Health stays 200 while the engine is unready. |
| `fake_engine.py` | 8789 | vLLM. OpenAI chat completions, buffered and SSE. `/ready` is 503 at zero replicas. |
| `telemetry.py` | | Gauges, traces, saturation, cost text. Prompt text is never stored. |
| `workload.py` | | Frozen card, format set, and `cost_per_thousand`. |
| `harness.py` | | Warm report and a separate cold report. |
| `index.html` | | Fleet dashboard: capacity, efficiency, model behavior. |

Fixture credentials are test doubles. `fixture-alpha` has 8 tokens so a normal
reserve returns 429. `fixture-beta` has room. A missing credential does not
move token counters. A 429 does not flip saturation. Those three rules carry
into production unchanged.

Two behaviors are fixture-only and must not be copied blindly:

- KV fill is `0.02 + 0.15 * in_flight`. The serving dashboard binds the same
  panel to vLLM's cache gauge in the cluster.
- Cost charges one spot node and one on-demand node for the whole process
  lifetime, at `$1.50` and `$3.00`, and the text says so. Production integrates
  only Ready GPU nodes, split by capacity type, at prices from a ConfigMap.
  The function stays `dollars / (tokens / 1000)`. Tokens are prompt plus
  completion. Zero tokens leaves the ratio undefined, and the dollars still
  show.

`door.py` still says hold versus refuse at zero replicas is unset. That choice
is closed in the next section, and it is closed in local code before any
cluster exists.

## Target shape

```
client
  → ALB (TLS, idle timeout 300s)
  → door  (CPU deployment, always on, no GPU toleration)
  → vLLM  (GPU deployment, 0..N, ClusterIP only)
```

Karpenter watches pending pods. KEDA watches a busy signal. They do different
jobs.

```
door_requests_waiting
door_requests_in_flight
vllm:num_requests_waiting
vllm:num_requests_running
        → sum as inference_busy
        → KEDA scales the vLLM Deployment
        → a pending GPU pod
        → Karpenter spot pool, else the on-demand pool
        → empty node past consolidateAfter is deleted
```

The door stays on a small CPU node so a request can arrive when every GPU is
gone. The GPU node is tainted `nvidia.com/gpu`. The door does not tolerate
that taint, so system pods and the door never sit on the GPU node. vLLM is the
only workload that does.

The ALB target group is the door. vLLM is not on the ALB. That is why the door
can be healthy while `/ready` on the engine is 503, which the harness already
checks.

### Instance

Llama 3.1 8B Instruct in BF16 is about 16 GB of weights. The first window uses
one `g6.xlarge` (one L4, 24 GB). `g5.xlarge` (one A10G, 24 GB) is in the same
pools as capacity fallback, not as a bigger card. There is no multi-GPU
instance.

vLLM arguments for the first window, frozen:

```
--model /models/Meta-Llama-3.1-8B-Instruct
--served-model-name llama-3.1-8b-instruct
--dtype bfloat16
--max-model-len 8192
--gpu-memory-utilization 0.90
--max-num-seqs 16
```

Continuous batching and paged attention are vLLM defaults. The local overlap
check (a short scripted reply finishes while a long one is still streaming)
proves the door does not serialize requests. It does not prove paged
attention. The window is where real time-to-first-token and inter-token
latency come from.

`max-model-len` is 8192, matching `MAX_TOKENS` in `telemetry.py`. A 128k
context on a 24 GB card spends the window on KV cache instead of the card.
If the pod OOMs on load, the fallback is `max-model-len` 4096 on the same
instance, in a later window. The first response to an OOM is not a larger GPU.

Production replica cap is 2. The first window's overlay sets `maxReplicaCount`
to 1, so the card cannot launch a second node. Saturation at the ceiling is
already proven locally.

### Weights

Stage the model to S3 from a CPU machine before the window. The GPU pod syncs
from that bucket in-region. The window does not download from Hugging Face.
The door image gets `tokenizer.json` and `tokenizer_config.json` only, so the
reserve uses the real Llama 3.1 tokenizer. Safetensors stay off the door.

The first cold start includes that S3 sync plus weight load. Say so on the
cold report. A later window that reuses a snapshot can separate sync time
from load time. The first window does not need a second measurement.

## Request path

`POST /v1/chat/completions` with `Authorization: Bearer <key>`.

1. The ALB terminates TLS and forwards to the door. Idle timeout is 300
   seconds so a stream survives. Health checks hit the door `/health`, which
   stays 200 at zero GPU replicas.
2. The door hashes the bearer token. Missing or unknown returns 401. Token
   counters do not move.
3. The door counts prompt tokens with the Llama tokenizer and reserves
   `prompt + max_tokens` against that client's bucket, and checks a request
   rate. A client who cannot cover the reserve gets 429
   `rate_limit_exceeded`. Saturation does not change. This is the local
   `try_reserve` behavior, with a refill added.
4. If no ready vLLM endpoint exists, or every ready replica is at its
   in-flight limit, the door increments `door_requests_waiting` and holds the
   connection.
5. The hold lasts up to the startup budget (15 minutes). Then the door
   returns 503, decrements the waiter, and releases the reserve. Holding
   forever pins a door thread and hides a stuck node. Refusing immediately
   turns every client retry into another scale attempt. Hold, then 503, is
   the policy. Implement it in `door.py` and assert it in the harness before
   AWS.
6. On admit, the door proxies the body to the vLLM Service and flushes each
   chunk. The local relay already does this. The door does not log the body.
   Access logs carry status, bytes, latency, and a hash of the key.
7. The door reads `usage` off the final chunk, the way `UsageCatcher` does,
   and settles the bucket to actual prompt plus completion tokens. Unused
   reserve returns to the bucket.

`/api/ready`, `/api/report`, and `/api/spot-reclaim` stay on the local edge.
They are not on the public door.

### Per-client limits

Two limits, both per key, both in the door:

- Requests per minute. Protects the door itself.
- A token bucket. Capacity and refill per minute live with the key. Reserve
  on admit, refund the unused portion on settle, refill on the clock.

A cluster-wide limit is not a substitute. One hot client would then 429
everyone else, and a 429 must not scale.

Keys are stored as hashes. The local names `fixture-alpha` and `fixture-beta`
remain test doubles and never ship as real keys.

## Scaling and cost

### The busy signal

Queue depth scales out. In-flight work keeps the replica. Both at zero scales
in.

```
inference_busy =
    sum(door_requests_waiting)
  + sum(door_requests_in_flight)
  + sum(vllm:num_requests_waiting)
  + sum(vllm:num_requests_running)
```

KEDA `ScaledObject` on the vLLM Deployment:

- `minReplicaCount: 0`
- `maxReplicaCount: 2` in the production manifest, `1` in the window overlay
- Prometheus trigger on `inference_busy`, threshold `1`
- `cooldownPeriod: 600` in production, `60` in the window overlay
- `pollingInterval: 10`

vLLM's own waiting metric does not exist until a pod is up, so scale-from-zero
depends on the door. Leaving in-flight out of the sum would scale a replica
away while a stream is still running.

CPU utilization and GPU utilization are not scale signals. A single decode
keeps a GPU busy, and that reading would block scale-to-zero.

### Karpenter

Two NodePools, same instance types (`g6.xlarge`, `g5.xlarge`), one GPU each.

| Pool | Weight | Capacity | Limit |
| --- | --- | --- | --- |
| `gpu-spot` | 50 | spot | 2 GPU |
| `gpu-ondemand` | 10 | on-demand | 2 GPU |

Karpenter tries the higher weight first. If spot cannot launch, the pending
pod falls through to on-demand. That on-demand node is a fallback for this
request, then it scales to zero with everything else. There is no warm
on-demand replica.

Disruption: `consolidationPolicy: WhenEmpty`. `consolidateAfter` is 5 minutes
in production and 30 seconds in the window overlay, so the proof of
scale-to-zero does not bill a production idle. `expireAfter` is 24 hours as a
backstop, which is longer than any window.

A small on-demand CPU node (a `t3.medium` is enough) runs the door, Karpenter,
the load balancer controller, and Prometheus. That node does not scale to
zero. Its cost is outside the per-thousand ratio, on the fleet footnote with
the EKS control plane, the ALB, and S3.

### Cost per thousand tokens

Same function as `workload.cost_per_thousand`:

```
ratio = gpu_dollars / (tokens / 1000)
```

`gpu_dollars` is spot node-hours times the spot price, plus on-demand
node-hours times the on-demand price. Node-hours come from Ready nodes
labeled as GPU nodes, split by `karpenter.sh/capacity-type`. Prices live in a
ConfigMap, read the morning of the window for `g6.xlarge` and `g5.xlarge`,
spot and on-demand. The laptop prices stay `$1.50` and `$3.00` and stay
labeled fixture prices.

Tokens are `increase(vllm:prompt_tokens_total)` plus
`increase(vllm:generation_tokens_total)` over the same window. The fleet page
shows the dollars even when the ratio is undefined, so an idle node that has
served nothing is visible.

Outside the ratio, written on the fleet page and not added in: EKS control
plane, the CPU node, the door, the ALB, S3, and data transfer. The local page
already has this line.

Check the four instance prices the morning of the window. A same-day cluster
that is deleted, with one GPU node inside a 20 minute budget, is a few
dollars. The failure mode that costs money is a cluster or a GPU node left
up. The timeout and the destroy are the control. Set an AWS budget alarm
before the first apply.

### The two empty choices on the fleet page

The page leaves Result and Choice blank for "warm floor versus scale-to-zero"
and "spot versus on-demand". The choices, made now:

- Scale to zero. A warm floor bills a GPU through idle. The measured
  cold-start time is the result that fills that note, and it stays out of the
  warm percentiles.
- Spot preferred, on-demand only when spot cannot launch. The result that
  fills the second note is the share of GPU hours on spot, plus whether any
  stream was cut. The first window does not simulate a reclaim. That drill is
  a later window.

The harness already rejects a report that tries to store `result` or `choice`.
Those fields stay empty until a person writes the measured numbers in. The
window does not auto-fill them.

## Reliability

### Probes

Startup probe gates liveness, so a slow load does not kill the pod.

| Probe | Path | Timing |
| --- | --- | --- |
| startup | vLLM `/health` | every 10s, 90 failures (15 minutes) |
| readiness | vLLM `/health` | every 5s, after startup succeeds |
| liveness | vLLM `/health` | every 15s, only after startup succeeds |
| door readiness | `/health` | ordinary, and it stays ready at zero GPUs |

Confirm on the pinned image that `/health` is not 200 before the weights are
loaded. If that image returns 200 early, point startup and readiness at a
check that `/v1/models` lists `llama-3.1-8b-instruct`.

`terminationGracePeriodSeconds` is 90. `preStop` sleeps 15 seconds so
endpoints drop before SIGTERM. vLLM then drains in-flight work until the
grace ends. The harness card's longest output is far inside 90 seconds.
Karpenter `WhenEmpty` will not remove a node that still has the pod.

Spot interruption uses the same drain. The two-minute warning can still cut a
long generation. That counter belongs on the serving dashboard when a later
window drills reclaim. It is not part of the first window.

### Readiness and the cold path

The sequence the cold report measures:

1. Replicas are 0. Door `/health` is 200. There is no vLLM pod.
2. One authenticated request arrives, passes the bucket, and waits.
3. `inference_busy` crosses 1. KEDA sets replicas to 1.
4. Karpenter launches a spot node.
5. The pod syncs weights and loads. Startup probe is red. The request is
   still held.
6. Readiness goes green. The door admits. The client sees the first token.
7. The cold report records time until first success, from the client's start,
   including the hold. It does not record a p50.

If step 6 has not happened 15 minutes after the node is Ready, the window
fails and destroys. It does not retry.

## Harness

One frozen card, two targets.

The card in `workload.py`: concurrency 4, prompt lengths 8 and 16, output
lengths 4 and 8, streaming on. The format set (first line, JSON keys) stays
the quality check. Local and GPU runs print the same two reports.

Warm report, unchanged:

- p50 and p95 of client end-to-end latency
- output tokens per second
- client time to first token, p50
- format pass count

Cold report, unchanged and never mixed into the warm percentiles:

- time until first success

Local-only checks, run on every push, not a reason to keep a GPU up:

- scripted completion lengths match the card
- a 429 leaves saturation quiet
- saturation turns on only at the ceiling with someone waiting, then turns off
- the cost formula, including an undefined ratio at zero tokens
- prompt text stays out of state, events, traces, and the page
- a report with extra `result` or `choice` is rejected
- the new case: a hold that outlives the budget returns 503 and the waiter
  count returns to zero

GPU target, `workflow_dispatch` only:

- the base URL is the ALB, not `127.0.0.1`
- there is no `/api/ready`. Cold means replicas are actually 0
- scripted length equality is skipped. The format shapes still have to match,
  and `usage` still has to arrive
- after the warm card, append four engine lines from Prometheus: TTFT p50,
  inter-token latency p50, queue wait p50, KV cache at the end of the card
- abort if the GPU node has been Ready for 20 minutes, if a second GPU node
  appears, or if startup is still failing 15 minutes after the node is Ready

GitHub:

- Pull requests run `serve.py` and `harness.py` only. No cloud credentials.
- `gpu-window.yml` is `workflow_dispatch`, protected by an environment, one
  concurrency group, `timeout-minutes: 45`.
- The job assumes an OIDC role. Destroy runs in `if: always()`.
- The role can read the weight bucket, manage this cluster, and not much else.

## Dashboards

Two dashboards. Fleet is the one already in `index.html`. Serving is new.
Serving is a separate page locally and a separate Grafana dashboard in the
cluster, not a fourth tab on Fleet. The fleet page answers a money question.
The serving page answers a latency question. Mixing them hides both.

Build the serving page against fixture series first, with the metric names
below, so the panels exist before the window. Point Grafana at the same names.

On the first boot inside the window, before the card, curl the pod's
`/metrics` once and bind panels to the names that image exports.
`vllm:gpu_cache_usage_perc` and `vllm:kv_cache_usage_perc` have both shipped.
Use the one that is present.

### Fleet

Already sketched by the page. Production sources replace fixture gauges.

- GPUs idle, people waiting, ready replicas, pending pods
- Spot GPUs and on-demand GPUs
- Tokens served, tokens per second
- GPU dollars in the window
- Cost per thousand tokens, spot and on-demand kept separate, ratio undefined
  at zero tokens
- Footnote: control plane, CPU door, ALB, weight storage
- Warm report glance and cold-start time, shown apart
- Saturation sentence, the same one as `workload.saturation_text`: quiet,
  unless ready replicas are at the ceiling and someone is waiting

### Serving

No dollar figure on this dashboard.

- Time to first token, p50 and p95 (`vllm:time_to_first_token_seconds`)
- Inter-token latency, p50 and p95 (`vllm:time_per_output_token_seconds`)
- End-to-end latency, p50 and p95 (`vllm:e2e_request_latency_seconds`). This
  is the harness p50 and p95, measured at the engine
- Queue wait, p50 and p95 (`vllm:request_queue_time_seconds`)
- Prefill time, p50 (`vllm:request_prefill_time_seconds`), so a slow first
  token can be split from queue wait
- KV cache utilization
- Preemptions (`vllm:num_preemptions_total`), the early sign the cache is tight
- Running versus waiting
- Tokens per second, from the generation counter
- Outcomes: 200, 401, 429, 503. The 429 series is a client series

Client TTFT from the harness includes the network, the door, and any hold.
Keep it on the warm report. The histogram is the engine number. Both are
worth having. They answer different questions.

Prefix-cache hit rate stays off until prefix caching is turned on. It is not
part of the first window.

### Alerts

Page:

- `inference_busy` has waiters and ready replicas have been at the cap for 2
  minutes. This is the saturation sentence.
- `door_requests_waiting > 0` and ready replicas are 0 for longer than the
  startup budget. A cold start is stuck, and a node may already be billing.
- KV cache above 0.90 for 5 minutes while requests are waiting.

Record, do not page:

- 401 and 429
- CPU or GPU utilization

Latency pages (TTFT p95, inter-token p95, queue-wait p95) get thresholds
after the window, from the numbers the card just measured. Setting them
before that measurement pages on a guess.

## Implementation phases

Eight phases. Phase 0 is the suite that already exists. Phases 1 through 7
run on the laptop, on kind, or on S3. Phase 8 is the only phase that launches
a GPU. A phase starts when the previous phase's command exits 0. Every phase
from 1 onward re-runs `python3 harness.py` and keeps that exit code at 0.

GPU money starts at phase 8. Phase 7 is the only earlier AWS spend, and it is
storage: the weight bucket. It does not launch a G-family or P-family instance.

### Phase 0 — Baseline

No new code. This is the regression suite the later phases are not allowed to break.

Run `python3 serve.py`, then `python3 harness.py`.

Pass when all of these are true:

- The harness exits 0, and stdout contains no line beginning `Check failed`.
- `reports/warm.txt` contains p50, p95, output tokens per second, client time to first token, and the format pass count.
- `reports/cold.txt` contains time until first success and does not contain `p50`.
- Stdout contains the overlap sentence, the quiet-429 sentence, the saturation sentence, and the formula line.
- The fleet page still has the timeline rows, the empty result and choice notes, and the formula check. `check_page` covers this.

Stop if this is red. Fix the baseline before changing the door.

### Phase 1 — Hold, then 503

Work in `door.py`, `telemetry.py`, and `harness.py`. The local edge grows `POST /api/hold-budget`, same class of fixture control as `/api/ready`. That route stays off the public door.

Default hold is long enough that today's cold test still beats it (30 seconds locally). The manifest later sets 15 minutes. The new test shortens the budget through `/api/hold-budget`.

Pass when `python3 harness.py` exits 0 and the new case inside it shows:

- With ready replicas at 0 and the hold budget under a second, one chat request returns 503.
- After that 503, people waiting is nobody, ready replicas are still 0, and saturation is quiet.
- Door `/health` is 200 and fake-engine `/ready` is 503.
- The caller's remaining budget is unchanged. The reserve was released.
- The existing cold case still returns 200, and `reports/cold.txt` still has no p50.
- The existing 429 case still leaves saturation quiet.

### Phase 2 — Metrics

Work in `telemetry.py`, `door.py`, and `fake_engine.py`. The door serves `GET /metrics`. The fake engine serves `GET /metrics` using the vLLM names the serving dashboard will bind to.

Door series: `door_requests_waiting`, `door_requests_in_flight`, `door_budget_rejections_total`, `door_requests_total` labeled by status code.

Engine series: `vllm:time_to_first_token_seconds`, `vllm:time_per_output_token_seconds`, `vllm:e2e_request_latency_seconds`, `vllm:request_queue_time_seconds`, `vllm:request_prefill_time_seconds`, `vllm:num_requests_running`, `vllm:num_requests_waiting`, `vllm:num_preemptions_total`, `vllm:prompt_tokens_total`, `vllm:generation_tokens_total`, and one cache gauge (`vllm:gpu_cache_usage_perc` or `vllm:kv_cache_usage_perc`).

Pass when the harness, still exit 0, also shows:

- While a request is held and not yet admitted, `door_requests_waiting` is 1 and `door_requests_in_flight` is 0.
- A finished admitted request returns in-flight to 0.
- One 429 increases `door_budget_rejections_total` by 1 and leaves `door_requests_waiting` at 0.
- One 401 increases `door_requests_total{code="401"}` and leaves rejections and waiting unchanged.
- After one streamed completion, the TTFT histogram count is at least 1.
- The sum of the four busy series equals door waiting plus door in-flight plus engine running plus engine waiting.
- `/metrics` on both ports contains none of the frozen prompt strings the leak scan already lists.

### Phase 3 — Serving page

New page, separate from Fleet. `serving.html` plus its script, registered on the local edge. It reads the live gauges. It is not a fourth tab on the fleet page.

Pass when the harness exits 0 and:

- `GET /serving` returns 200.
- The body names time to first token, inter-token latency, end-to-end latency, queue wait, KV cache, running, waiting, and tokens per second.
- The body has no `per thousand`, no `node hours`, and no `Formula check`.
- `GET /` still passes `check_page`, including the cost formula and the footnote.
- After the warm card, the serving page shows a time-to-first-token value. The empty state is gone.
- In a browser, `/` still answers GPUs idle and people waiting, and `/serving` shows the latency panels with no cost block.

### Phase 4 — Manifests

Add `deploy/` for the door, vLLM, the two Karpenter pools, the KEDA ScaledObject, the ingress, the ServiceMonitor, the Prometheus rules, `grafana/fleet.json`, `grafana/serving.json`, and `overlays/window`. A local checker (`python3 check_manifests.py`, with kubeconform) reads those files. No cluster, no image pull.

Pass when that checker exits 0 and reports:

- kubeconform accepts every document, including the KEDA and Karpenter kinds.
- The vLLM Deployment's replicas are 0, it requests `nvidia.com/gpu: 1`, and it tolerates the GPU taint.
- Startup probe `failureThreshold * periodSeconds` is at least 900. `preStop` and `terminationGracePeriodSeconds` are set.
- The door requests no GPU and carries no GPU toleration.
- The only Ingress backend is the door Service. The idle-timeout annotation is 300 seconds. No backend names the vLLM Service.
- The ScaledObject has `minReplicaCount` 0 and `maxReplicaCount` 2, and its query contains all four busy metric names.
- The spot pool's weight is greater than the on-demand pool's weight. Both list `g6.xlarge` and `g5.xlarge`.
- `overlays/window` sets `maxReplicaCount` 1, `cooldownPeriod` 60, and `consolidateAfter` 30s.
- The Prometheus rules include saturation, stuck cold start, and KV cache above 0.90 with waiters. No rule pages on 429 alone.
- `serving.json` names TTFT and KV cache and does not name a cost ratio. `fleet.json` names cost per thousand.

### Phase 5 — Scaler on kind

Docker is required. Build the door and the fake engine as ordinary CPU images. Install Prometheus and KEDA into a local kind cluster. Apply the window overlay so cooldown is short. Do not apply the Karpenter pools. kind has no GPU, and those pools must not be created.

Pass when `python3 kind_scale.py` exits 0 and then deletes the kind cluster:

- Before any request, the door is ready and the engine Deployment has 0 ready replicas.
- One authorized request returns 200, and the engine Deployment is observed at 1 replica before the response finishes.
- After the short cooldown, ready replicas return to 0.
- A 429 sent while replicas are 0 leaves replicas at 0 for the following 20 seconds.
- The script's kubeconfig is the kind cluster. It does not call AWS.

The GPU taint split is not claimed here. kind has one node. Phase 4 checks the toleration fields. Phase 8 checks that the door pod landed on the CPU node and vLLM landed on the GPU node.

### Phase 6 — Images, tokenizer, workflows

Build the door image with `tokenizer.json` and `tokenizer_config.json` only. Pin the upstream vLLM image tag in the manifest. Do not pull that image and do not run it.

Add `.github/workflows/local.yml` and `.github/workflows/gpu-window.yml`. `local.yml` runs the harness and `check_manifests.py` on pull requests. It does not run kind on every pull request unless the runner already has Docker. Kind stays a command a person runs at the end of phase 5, and again before phase 8.

Pass when:

- The door image builds.
- A one-shot container prints a token count for a fixed string and exits 0. A second run prints the same count.
- The image contains no `*.safetensors`.
- `local.yml` triggers on `pull_request` and has no GPU job.
- `gpu-window.yml` triggers only on `workflow_dispatch`, sets `timeout-minutes` to 45 or less, sets a concurrency group, and has a destroy step with `if: always()`.
- `gpu-window.yml` is not executed in this phase.

### Phase 7 — Weights in S3

First use of the AWS account. An account budget alarm is already on. Copy the model from a laptop or a `t3` machine into the bucket. The GPU window syncs from this bucket.

Pass when:

- The bucket prefix contains `config.json`, the tokenizer files, and the weight shards.
- The object sizes sum to at least 15 GB.
- `aws ec2 describe-instances` in that region shows no running `g4`, `g5`, `g6`, or `p` instance.
- A CPU process can read `config.json` back from the bucket.

Stop if a GPU instance appeared. Terminate it before phase 8, and treat phase 7 as failed.

### Phase 8 — GPU window

This phase is the deployment. Its runbook is the next section. Start it only when phases 0 through 7 are green, the four prices are in the ConfigMap, and a person dispatches `gpu-window.yml`.

Pass when all of these are true:

- `reports/warm.txt` and `reports/cold.txt` were produced against the ALB.
- The cold report has time until first success and no p50.
- The warm report has p50, p95, output tokens per second, client time to first token, and a format pass count equal to the format set. It also has engine TTFT p50, inter-token latency p50, queue wait p50, and the KV cache reading from the end of the card.
- The door pod is on a CPU node. The vLLM pod was on a GPU node.
- `/metrics` on that pod exposed a TTFT histogram, an inter-token histogram, and a cache gauge, and the dashboard was bound to those names before the card ran.
- Ready replicas are 0 and no GPU node remains.
- The GPU node was Ready for under 20 minutes. A second GPU node never appeared.
- The cluster delete finished. The job log shows the destroy step ran.

Fail the window, destroy, and do not dispatch it again automatically when the startup probe is still failing 15 minutes after the node is Ready, when a second GPU node appears, or when the node has been Ready for 20 minutes.

## The GPU window

Phase 8. Do this in one sitting. Create the cluster that morning. Delete it
before the end of the day, in the job's `always()` path, not by memory.

Before apply: budget alarm is on, weights are in S3, prices are in the
ConfigMap, the overlay sets `maxReplicaCount: 1`, `cooldownPeriod: 60`, and
`consolidateAfter: 30s`.

| Step | GPU node | Stop if |
| --- | --- | --- |
| Create the cluster, install Karpenter, the load balancer controller, KEDA, Prometheus, Grafana | Not yet | |
| Apply pools and the deployment at 0 replicas | Not yet | |
| Curl `/metrics` once the pod exists, bind names | Starting | Names for TTFT, inter-token latency, or the cache gauge are absent |
| Cold: one request from replicas 0, write the cold report | Yes | Startup still failing 15 minutes after the node is Ready |
| Warm: the frozen card, write the warm report plus the four engine lines | Yes | A second GPU node appears |
| Wait out the short cooldown and confirm replicas 0 and the node is gone | Yes, until it leaves | The node is still up at 20 minutes Ready |
| Destroy the cluster | No | The job's 45 minute wall clock, which destroys anyway |

Planned GPU-billed time inside that 20 minutes: about 4 for launch, 6 for the
weight sync and load, 2 for the card, 5 to watch scale-to-zero, 3 of buffer.
Cluster create is control-plane time and is not GPU time. It still bills the
control plane until destroy.

The harness writes `reports/warm.txt` and `reports/cold.txt` as it does now.
Grab those and a Grafana snapshot. Then the account should show no GPU nodes
and, once destroy finishes, no cluster.

## Decisions

1. The door is always on. vLLM scales to zero. The ALB targets the door only.
2. At zero replicas the door holds up to the startup budget, then returns 503.
3. The scale signal is waiting plus in-flight, at the door and at vLLM. CPU
   and GPU utilization are not scale signals.
4. Spot is preferred. On-demand is the fallback for a pod spot could not
   place. There is no warm spare.
5. One `g6.xlarge` for the window. Production cap is two replicas. The window
   cap is one.
6. `max-model-len` is 8192. An OOM on load leads to 4096 on the same card, in
   a later window.
7. Cost per thousand uses GPU node-hours only, prompt plus completion tokens,
   and the existing formula. Fixed costs stay on the footnote.
8. Fleet and Serving are separate dashboards.
9. 401 and 429 never scale and never page.
10. Pull requests run the local harness. The GPU workflow is manual, single
    concurrency, and always destroys.
11. Scale-to-zero and spot-preferred are the choices for the two empty notes.
    The measured results are written in after the window, by a person.

## Out of the first window

These are real follow-ups. Each one is its own dispatch, after the first
reports exist.

- A warm floor, only if the measured cold start misses a limit set from that
  measurement.
- A spot-reclaim drill, and the streams-cut counter.
- A second replica, to watch queue-depth scale from 1 to 2.
- Quantization, prefix caching, and a longer `max-model-len`.
- Latency alert thresholds, taken from the warm report.
- Moving the CPU node into a private subnet. The window can use a public
  subnet with a tight security group so the GPU node does not sit behind a
  NAT gateway. Production can change that later without changing the door.
