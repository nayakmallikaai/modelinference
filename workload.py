"""Frozen workload card, format set, and the cost formula.

Prompt text stays in this module so the door can recognize a frozen message
and forward a script id. Reports, events, and the screen copy the id and the
counts, not the message.
"""

import json
from decimal import Decimal

CONCURRENCY = 4
PROMPT_LENGTHS = (8, 16)
OUTPUT_LENGTHS = (4, 8)
STREAMING = True
REPLICA_CEILING = 4

SPOT_NODE_PRICE = Decimal("1.50")
ON_DEMAND_NODE_PRICE = Decimal("3.00")

SATURATION_QUIET = "Saturation: quiet."
SATURATION_FULL = "Saturation: ready replicas are at the ceiling and work is still waiting."


def _words(count, word):
    return " ".join([word] * count)


CARD_PROMPTS = (
    {
        "id": "out-4",
        "messages": [{"role": "user", "content": _words(PROMPT_LENGTHS[0], "tok")}],
        "max_tokens": OUTPUT_LENGTHS[0],
    },
    {
        "id": "out-8",
        "messages": [{"role": "user", "content": _words(PROMPT_LENGTHS[1], "tok")}],
        "max_tokens": OUTPUT_LENGTHS[1],
    },
)

FORMAT_SET = (
    {
        "id": "line",
        "messages": [{"role": "user", "content": "First line named-line"}],
        "kind": "first_line",
        "named": "named-line",
        "max_tokens": 8,
    },
    {
        "id": "keys",
        "messages": [{"role": "user", "content": "Keys named alpha beta"}],
        "kind": "keys",
        "named": ("alpha", "beta"),
        "max_tokens": 8,
    },
)

SHORT = {
    "id": "short",
    "messages": [{"role": "user", "content": "short fixture script"}],
    "max_tokens": 2,
}

LONG = {
    "id": "long",
    "messages": [{"role": "user", "content": "long fixture script"}],
    "max_tokens": 24,
}

PLANS = {
    "line": {"pieces": ["named-line"], "gap": 0.01},
    "keys": {"pieces": ['{"alpha":1,"beta":2}'], "gap": 0.01},
    "short": {"count": 2, "gap": 0.01, "word": "s"},
    "long": {"count": 24, "gap": 0.08, "word": "L"},
    "out-4": {"count": 4, "gap": 0.012, "word": "w"},
    "out-8": {"count": 8, "gap": 0.012, "word": "w"},
}

_FROZEN = CARD_PROMPTS + FORMAT_SET + (SHORT, LONG)


def script_for(payload):
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return ""
    for item in _FROZEN:
        if messages == item["messages"]:
            return item["id"]
    return ""


def plan_pieces(script, max_tokens):
    plan = PLANS.get(script)
    if plan is None:
        return None
    if "pieces" in plan:
        return list(plan["pieces"][:max_tokens]), plan["gap"]
    count = min(plan["count"], max_tokens)
    word = plan["word"]
    return ["%s%d" % (word, index) for index in range(count)], plan["gap"]


def card_batch():
    by_output = {item["max_tokens"]: item for item in CARD_PROMPTS}
    return [by_output[OUTPUT_LENGTHS[0]], by_output[OUTPUT_LENGTHS[1]], by_output[OUTPUT_LENGTHS[0]], by_output[OUTPUT_LENGTHS[1]]]


def shape_matches(reply, item):
    if not isinstance(reply, str) or not reply.strip():
        return False
    if item["kind"] == "first_line":
        lines = [line.strip() for line in reply.splitlines() if line.strip()]
        return bool(lines) and lines[0] == item["named"]
    if item["kind"] == "keys":
        try:
            data = json.loads(reply)
        except json.JSONDecodeError:
            return False
        return isinstance(data, dict) and all(key in data for key in item["named"])
    return False


def cost_per_thousand(dollars, tokens):
    dollars = Decimal(dollars)
    tokens = Decimal(tokens)
    if tokens <= 0:
        return None
    return dollars / (tokens / Decimal(1000))


def formula_check_line():
    value = cost_per_thousand(Decimal("2"), Decimal("400000"))
    shown = "not defined" if value is None else format(value, "f")
    return "Formula check only. $2 of GPU and 400,000 tokens is $%s per thousand. Not the fleet ratio." % shown


def saturation_text(ready, waiting, ceiling):
    if ready >= ceiling and waiting > 0:
        return SATURATION_FULL
    return SATURATION_QUIET


def describe_cost(elapsed_seconds, tokens):
    seconds = Decimal(str(elapsed_seconds)).quantize(Decimal("0.001"))
    if seconds < 0:
        seconds = Decimal("0.000")
    hours = (seconds / Decimal(3600)).quantize(Decimal("0.000001"))
    spot_dollars = (hours * SPOT_NODE_PRICE).quantize(Decimal("0.000001"))
    demand_dollars = (hours * ON_DEMAND_NODE_PRICE).quantize(Decimal("0.000001"))
    total_dollars = (spot_dollars + demand_dollars).quantize(Decimal("0.000001"))
    tokens = max(0, int(tokens))
    spot_ratio = cost_per_thousand(spot_dollars, tokens)
    demand_ratio = cost_per_thousand(demand_dollars, tokens)
    total_ratio = cost_per_thousand(total_dollars, tokens)

    def shown(ratio):
        if ratio is None:
            return "not defined"
        return "$" + format(ratio.quantize(Decimal("0.000001")), "f")

    if total_ratio is None:
        sum_line = "Sum: not defined. Tokens served: 0. Fixture prices, not a cloud price."
        exact = None
        status = "no tokens in the window"
        value = None
    else:
        exact_ratio = total_ratio.quantize(Decimal("0.000001"))
        exact = format(exact_ratio, "f")
        sum_line = "Sum: $%s / (%d / 1000) = $%s per thousand. Fixture prices, not a cloud price." % (
            format(total_dollars, "f"),
            tokens,
            exact,
        )
        status = "fixture, spot and on-demand summed"
        value = float(exact_ratio)
    text = "\n".join([
        "Spot: %s node hours × $%s fixture = $%s. Per thousand: %s." % (
            format(hours, "f"),
            format(SPOT_NODE_PRICE, "f"),
            format(spot_dollars, "f"),
            shown(spot_ratio),
        ),
        "On-demand: %s node hours × $%s fixture = $%s. Per thousand: %s." % (
            format(hours, "f"),
            format(ON_DEMAND_NODE_PRICE, "f"),
            format(demand_dollars, "f"),
            shown(demand_ratio),
        ),
        sum_line,
    ])
    return {"text": text, "value": value, "exact": exact, "status": status}
