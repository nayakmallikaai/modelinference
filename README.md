# LLM inference serving

Llama 3.1 8B Instruct behind an OpenAI-compatible door. The laptop runs the whole request path. AWS is a short, manual GPU window at the end.

```
client → door (CPU, always on) → vLLM (GPU, scales to zero)
```

The door does auth and the token budget. vLLM is the only pod that tolerates the GPU taint. Scale follows waiting plus in-flight work, at the door and at vLLM. A 401 or a 429 does not scale and does not page.

## What is ready

These pass on this machine without an AWS account:

| Command | What it proves |
| --- | --- |
| `python3 serve.py` then `python3 harness.py` | Hold-then-503, metrics, the serving page, the cost formula, and the frozen card |
| `python3 check_manifests.py` | Manifests, including KEDA and Karpenter, match the design |
| `python3 kind_scale.py` | On a local kind cluster, one request scales the engine 0 → 1 → 0, and a 429 does not. Deletes the cluster |
| `python3 check_image.py` | The door image builds with the Llama tokenizer and no weight files |

`gpu-window.yml` is `workflow_dispatch` only. It is not run by pull requests. A dispatch does nothing until the GitHub variable `GPU_WINDOW_CONFIRM` is `yes`.

Phase 7, copying weights to S3, has not been run. This machine has no working AWS profile. Phase 8, the GPU window, has not been dispatched.

## Run it locally

```bash
python3 serve.py
```

Open http://127.0.0.1:8787/ for Fleet and http://127.0.0.1:8787/serving for latency. In another terminal:

```bash
python3 harness.py
```

Fixture credentials are test doubles. `fixture-beta` has room. `fixture-alpha` has 8 tokens, so a normal reserve returns 429.

The door image counts tokens with the Llama 3.1 tokenizer in `tokenizer/`. Those two files are Llama materials under the Llama Community License. Weight files are not in this repo.

## AWS, when the account exists

Do this before `python3 stage_weights.py`:

1. Root account with MFA, and a monthly budget alarm (a $25 budget with mail at 50% and 100% is enough to start).
2. An IAM user for the laptop that can read EC2 instance state and write one `inference-weights-*` bucket. It does not need permission to start instances.
3. `aws configure --profile inference` in `us-east-1`, then `aws sts get-caller-identity` succeeds.
4. Llama 3.1 8B Instruct downloaded to a folder on this CPU. Set `MODEL_DIR` to that folder. Set `WEIGHT_BUCKET` if you want a name other than `inference-weights-<account id>`.

```bash
export AWS_PROFILE=inference
export AWS_REGION=us-east-1
export MODEL_DIR=/path/to/Meta-Llama-3.1-8B-Instruct
python3 stage_weights.py
```

The script stops if a `g4`, `g5`, `g6`, or `p` instance is running or pending. It uploads `config.json`, the tokenizer, and the shards, then reads `config.json` back.

## GPU window

Dispatch `.github/workflows/gpu-window.yml` only after the bucket check is green, the budget alarm is on, and you have typed the four prices for that morning (`g6.xlarge` and `g5.xlarge`, spot and on-demand).

Repository variables:

| Variable | Value |
| --- | --- |
| `GPU_WINDOW_CONFIRM` | `yes` on the morning of the window, cleared after |
| `GPU_WINDOW_ROLE_ARN` | OIDC role trusted by this repository |
| `AWS_REGION` | `us-east-1` |
| `WEIGHT_BUCKET` | the bucket from phase 7 |
| `PRICE_G6_SPOT` `PRICE_G6_ONDEMAND` `PRICE_G5_SPOT` `PRICE_G5_ONDEMAND` | dollars per hour that morning |

Protect the GitHub environment `gpu-window` with a required reviewer. The job times out at 45 minutes. Destroy runs even when the window fails. The script deletes the cluster named `inference` and leaves every other instance alone.

`deploy/scripts/window.py` is the same command locally:

```bash
export GPU_WINDOW_CONFIRM=yes
export AWS_REGION=us-east-1
export WEIGHT_BUCKET=inference-weights-123456789012
export PRICE_G6_SPOT=...
export PRICE_G6_ONDEMAND=...
export PRICE_G5_SPOT=...
export PRICE_G5_ONDEMAND=...
python3 gpu_window.py run
python3 gpu_window.py destroy
```

The window uses one CPU node (`t3.large`) for the door and lets Karpenter add one GPU node (`g6.xlarge`, with `g5.xlarge` as capacity fallback). The vLLM pod syncs weights from S3 before it loads. The window overlay caps replicas at 1, cooldown at 60 seconds, and `consolidateAfter` at 30 seconds. It aborts when a second GPU node appears, when startup is still failing 15 minutes after the node is Ready, or when that node has been Ready for 20 minutes.

## Layout

| Path | Role |
| --- | --- |
| `serve.py` `door.py` `fake_engine.py` | Local ALB, door, and engine |
| `harness.py` | Warm report and a separate cold report |
| `deploy/` | Cluster manifests. `overlays/window` is the short, one-replica window |
| `kind/` | CPU stand-in used by `kind_scale.py`. Karpenter is not applied there |
| `deploy/scripts/window.py` | Create the window, run the card, delete the cluster |
| `stage_weights.py` | Copy weights to S3 from a CPU |
| `.github/workflows/local.yml` | Pull requests: harness and manifest check |
| `.github/workflows/gpu-window.yml` | Manual window. Always destroys |
| `DESIGN.md` | The design the checks are written against |
