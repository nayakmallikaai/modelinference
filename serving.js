"use strict";

function textOf(id, value) {
  var node = document.getElementById(id);
  if (node) node.textContent = value;
}

function latencyText(pair) {
  if (!pair || pair.p50 === null || pair.p50 === undefined) return "No finished requests";
  var p50 = Number(pair.p50);
  var p95 = Number(pair.p95);
  if (!Number.isFinite(p50) || !Number.isFinite(p95)) return "No finished requests";
  return p50 + " ms p50, " + p95 + " ms p95";
}

function statusText(pair) {
  if (!pair || typeof pair.status !== "string" || pair.status.length === 0 || pair.status.length > 80) {
    return "status missing";
  }
  return pair.status;
}

function paint(data) {
  textOf("ttft-value", latencyText(data.timeToFirstToken));
  textOf("ttft-status", statusText(data.timeToFirstToken));
  textOf("itl-value", latencyText(data.interTokenLatency));
  textOf("itl-status", statusText(data.interTokenLatency));
  textOf("e2e-value", latencyText(data.endToEnd));
  textOf("e2e-status", statusText(data.endToEnd));
  textOf("queue-value", latencyText(data.queueWait));
  textOf("queue-status", statusText(data.queueWait));
  textOf("prefill-value", latencyText(data.prefill));
  textOf("prefill-status", statusText(data.prefill));

  var kv = data.kvCache || {};
  var kvNumber = Number(kv.value);
  textOf("kv-value", Number.isFinite(kvNumber) ? Math.round(kvNumber * 100) + "%" : "—");
  textOf("kv-status", statusText(kv));

  textOf("running-value", Number.isFinite(Number(data.running)) ? String(data.running) : "0");
  textOf("waiting-value", Number.isFinite(Number(data.waiting)) ? String(data.waiting) : "0");

  var rate = data.tokensPerSecond || {};
  var rateNumber = Number(rate.value);
  textOf("rate-value", Number.isFinite(rateNumber) ? rateNumber + " tokens/s" : "—");
  textOf("rate-status", statusText(rate));

  var outcomes = data.outcomes || {};
  ["200", "401", "429", "503"].forEach(function (code) {
    var count = Number(outcomes[code]);
    textOf("out-" + code, Number.isFinite(count) ? String(count) : "0");
  });
  textOf("serving-notice", "Live serving metrics on this computer.");
}

function fail() {
  textOf("serving-notice", "Serving metrics did not load.");
}

function loop() {
  fetch("/api/serving", { cache: "no-store" }).then(function (response) {
    if (!response.ok) throw new Error("serving");
    return response.json();
  }).then(paint).catch(fail).then(function () {
    window.setTimeout(loop, 400);
  });
}

loop();
