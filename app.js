"use strict";

var FIXTURE_URL = {
  idle: "fixtures/idle.json",
  busy: "fixtures/busy.json",
};

var VIEWS = ["fleet", "efficiency", "behavior"];

var VIEW_LABEL = {
  fleet: "Fleet capacity",
  efficiency: "Efficiency",
  behavior: "Model behavior",
};

var TABLES = {
  fleet: {
    bodyId: "fleet-rows",
    source: function (data) { return data.fleet; },
    rows: [
      ["readyGpus", "Ready GPUs", formatCount],
      ["idleGpus", "Idle GPUs", formatCount],
      ["waitingRequests", "Waiting requests", formatCount],
      ["pendingPods", "Pending pods", formatCount],
      ["spotGpus", "Spot GPUs", formatCount],
      ["onDemandGpus", "On-demand GPUs", formatCount],
    ],
  },
  efficiency: {
    bodyId: "efficiency-rows",
    source: function (data) { return data.efficiency; },
    rows: [
      ["tokensPerSecond", "Tokens per second", formatTokens],
      ["costPerThousandTokens", "Cost per thousand tokens", formatCost],
      ["kvFill", "KV fill", formatShare],
    ],
  },
  behavior: {
    bodyId: "behavior-rows",
    source: function (data) { return data.behavior; },
    rows: [
      ["timeToFirstToken", "Time to first token", formatMs],
      ["interTokenLatency", "Inter-token latency", formatMs],
    ],
  },
};

var loadSeq = 0;

function formatCount(gauge) {
  var n = Number(gauge.value);
  return Number.isFinite(n) ? String(n) : "—";
}

function formatTokens(gauge) {
  var n = Number(gauge.value);
  if (!Number.isFinite(n)) return "—";
  var text = Number.isInteger(n) ? String(n) : n.toFixed(2);
  return text + " tokens/s";
}

function formatCost(gauge) {
  if (gauge.value === null || gauge.value === undefined) return "—";
  var n = Number(gauge.value);
  return Number.isFinite(n) ? "$" + n.toFixed(3) + " fixture" : "—";
}

function formatShare(gauge) {
  var n = Number(gauge.value);
  return Number.isFinite(n) ? Math.round(n * 100) + "%" : "—";
}

function formatMs(gauge) {
  if (gauge.value === null || gauge.value === undefined) return "none";
  var n = Number(gauge.value);
  return Number.isFinite(n) ? String(n) + " ms" : "—";
}

function statusText(gauge) {
  if (!gauge || typeof gauge.status !== "string") return "status missing";
  var text = gauge.status.replace(/\s+/g, " ").trim();
  if (!text || text.length > 80) return "status missing";
  return text;
}

function sentence(value, prefix) {
  if (typeof value !== "string") return prefix + "unavailable";
  var text = value.replace(/\s+/g, " ").trim();
  if (text.indexOf(prefix) !== 0 || text.length > 80) return prefix + "unavailable";
  return text;
}

function showView(name) {
  VIEWS.forEach(function (view) {
    var on = view === name;
    var panel = document.getElementById("panel-" + view);
    var tab = document.getElementById("tab-" + view);
    panel.hidden = !on;
    tab.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.getElementById("current-view").textContent = "Current view: " + VIEW_LABEL[name];
}

function clearTables() {
  VIEWS.forEach(function (view) {
    document.getElementById(TABLES[view].bodyId).replaceChildren();
  });
}

function fillTable(view, data) {
  var spec = TABLES[view];
  var source = spec.source(data) || {};
  var tbody = document.getElementById(spec.bodyId);
  var fragment = document.createDocumentFragment();
  spec.rows.forEach(function (row) {
    var key = row[0];
    var label = row[1];
    var format = row[2];
    var gauge = source[key];
    var tr = document.createElement("tr");
    var nameCell = document.createElement("th");
    nameCell.scope = "row";
    nameCell.textContent = label;
    var valueCell = document.createElement("td");
    var statusCell = document.createElement("td");
    statusCell.className = "status";
    if (!gauge || typeof gauge.status !== "string" || gauge.status.length === 0) {
      valueCell.textContent = "—";
      statusCell.textContent = "status missing";
    } else {
      valueCell.textContent = format(gauge);
      statusCell.textContent = statusText(gauge);
    }
    tr.append(nameCell, valueCell, statusCell);
    fragment.append(tr);
  });
  tbody.replaceChildren(fragment);
}

function showUnavailable() {
  document.getElementById("gpus-idle").textContent = "GPUs idle: unavailable";
  document.getElementById("people-waiting").textContent = "People waiting: unavailable";
  document.getElementById("fixture-notice").textContent =
    "The fixture file did not load. Nothing on this page is live.";
  clearTables();
  document.title = "Fleet";
}

function afterSentencesPainted(seq, data, textAssignedMs) {
  requestAnimationFrame(function () {
    requestAnimationFrame(function () {
      if (seq !== loadSeq) return;
      if (!window.__fleetPageLoad && typeof data.gpusIdle === "string" && typeof data.peopleWaiting === "string") {
        performance.mark("sentences-readable");
        var mark = performance.getEntriesByName("sentences-readable").pop();
        window.__fleetPageLoad = {
          sentencesReadableMs: Math.round(mark.startTime),
          textAssignedMs: Math.round(textAssignedMs),
          visibility: document.visibilityState,
          sentences: [
            document.getElementById("gpus-idle").textContent,
            document.getElementById("people-waiting").textContent,
          ],
        };
      }
      VIEWS.forEach(function (view) { fillTable(view, data); });
      if (window.__fleetPageLoad && window.__fleetPageLoad.tableFilledMs == null) {
        window.__fleetPageLoad.tableFilledMs = Math.round(performance.now());
      }
    });
  });
}

function paintSentences(data, name) {
  document.getElementById("gpus-idle").textContent = sentence(data.gpusIdle, "GPUs idle: ");
  document.getElementById("people-waiting").textContent = sentence(data.peopleWaiting, "People waiting: ");
  var label = typeof data.fixtureLabel === "string" && data.fixtureLabel.length > 0 && data.fixtureLabel.length < 80
    ? data.fixtureLabel
    : name;
  var notice = typeof data.notice === "string" && data.notice.length > 0 && data.notice.length < 120
    ? data.notice
    : "Fixture numbers. Not a live fleet.";
  document.getElementById("fixture-notice").textContent = notice + " Showing: " + label + ".";
  document.title = "Fleet — " + label;
  document.body.dataset.fixture = typeof data.fixture === "string" ? data.fixture : name;
}

async function loadFixture(name) {
  var seq = ++loadSeq;
  var main = document.querySelector("main");
  if (!Object.prototype.hasOwnProperty.call(FIXTURE_URL, name)) {
    showUnavailable();
    main.removeAttribute("aria-busy");
    return;
  }
  main.setAttribute("aria-busy", "true");
  try {
    var response = await fetch(FIXTURE_URL[name], { cache: "no-store" });
    if (!response.ok) throw new Error("Fixture request failed");
    var data = await response.json();
    if (seq !== loadSeq) return;
    clearTables();
    paintSentences(data, name);
    main.removeAttribute("aria-busy");
    var textAssignedMs = performance.now();
    afterSentencesPainted(seq, data, textAssignedMs);
  } catch (error) {
    if (seq !== loadSeq) return;
    showUnavailable();
  } finally {
    if (seq === loadSeq) main.removeAttribute("aria-busy");
  }
}

function moveTab(event) {
  var tabs = VIEWS.map(function (view) { return document.getElementById("tab-" + view); });
  var index = tabs.indexOf(document.activeElement);
  if (index === -1) return;
  var next = index;
  if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % tabs.length;
  else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index - 1 + tabs.length) % tabs.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = tabs.length - 1;
  else return;
  event.preventDefault();
  tabs[next].focus();
  showView(VIEWS[next]);
}

VIEWS.forEach(function (view) {
  var tab = document.getElementById("tab-" + view);
  tab.addEventListener("click", function () { showView(view); });
  tab.addEventListener("focus", function () { showView(view); });
});

document.getElementById("view-nav").addEventListener("keydown", moveTab);

document.querySelectorAll('input[name="fixture"]').forEach(function (input) {
  input.addEventListener("change", function () {
    if (input.checked) loadFixture(input.value);
  });
});

var EVENT_FIELDS = [
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
];

function eventLine(event) {
  var parts = [];
  EVENT_FIELDS.forEach(function (key) {
    if (!event || event[key] === undefined || event[key] === null) return;
    parts.push(key + "=" + event[key]);
  });
  return parts.join(" ");
}

function renderEvents(events) {
  var lines = Array.isArray(events) ? events.map(eventLine) : [];
  document.getElementById("event-log").textContent = lines.join("\n");
}

function renderTraces(traces) {
  var blocks = (Array.isArray(traces) ? traces : []).map(function (trace) {
    var spans = (trace.spans || []).map(function (span) { return span.name; }).join(" → ");
    var head = (trace.traceId || "") + (trace.sampled ? " sampled" : "") + " " + spans;
    var lines = (trace.events || []).map(eventLine);
    return head + (lines.length ? "\n" + lines.join("\n") : "");
  });
  document.getElementById("trace-view").textContent = blocks.join("\n\n");
}

function applyLive(data, seq) {
  paintSentences(data, "live");
  var served = document.getElementById("tokens-served");
  var rejections = document.getElementById("budget-rejections");
  if (typeof data.tokensServed === "number") {
    served.hidden = false;
    served.textContent = "Tokens served: " + data.tokensServed;
  }
  if (typeof data.budgetRejections === "number") {
    rejections.hidden = false;
    rejections.textContent = "Budget rejections: " + data.budgetRejections;
  }
  var readyNow = document.getElementById("ready-now");
  if (typeof data.readyReplicas === "number") {
    readyNow.textContent = "Ready now: " + data.readyReplicas;
  }
  if (!window.__fleetPageLoad) {
    var textAssignedMs = performance.now();
    afterSentencesPainted(seq, data, textAssignedMs);
  } else {
    VIEWS.forEach(function (view) { fillTable(view, data); });
  }
  renderPhase3(data);
}

var SATURATION_TEXT = {
  "Saturation: quiet.": true,
  "Saturation: ready replicas are at the ceiling and work is still waiting.": true,
};

function roundText(value) {
  var n = Number(value);
  if (!Number.isFinite(n)) return null;
  return String(Math.round(n * 1000) / 1000);
}

function numberList(value) {
  if (!Array.isArray(value) || value.length === 0 || value.length > 8) return null;
  var out = [];
  for (var i = 0; i < value.length; i += 1) {
    if (typeof value[i] !== "number" || !Number.isFinite(value[i])) return null;
    out.push(String(value[i]));
  }
  return out.join(", ");
}

function cardText(report) {
  var prompts = numberList(report.promptLengths);
  var outputs = numberList(report.outputLengths);
  var concurrency = roundText(report.concurrency);
  if (!prompts || !outputs || !concurrency) return null;
  var streaming = report.streaming ? "yes" : "no";
  return "Workload card. Concurrency " + concurrency + ". Prompt lengths " + prompts + ". Output lengths " + outputs + ". Streaming " + streaming + ".";
}

function latencyLine(report) {
  var p50 = roundText(report.p50Ms);
  var p95 = roundText(report.p95Ms);
  var rate = roundText(report.outputTokensPerSecond);
  var client = roundText(report.clientTimeToFirstTokenMs);
  var matched = roundText(report.qualityMatched);
  if (p50 === null || p95 === null || rate === null || client === null || matched === null) return null;
  return "p50 " + p50 + " ms. p95 " + p95 + " ms. Output tokens per second " + rate + ". Client time to first token " + client + " ms. Pass count: " + matched + ".";
}

function renderPhase3(data) {
  var saturation = document.getElementById("saturation");
  if (typeof data.saturation === "string" && SATURATION_TEXT[data.saturation]) {
    saturation.hidden = false;
    saturation.textContent = data.saturation;
  }
  var cost = document.getElementById("cost-detail");
  if (typeof data.costDetail === "string" && data.costDetail.length < 800 && data.costDetail.indexOf("Spot:") === 0 && data.costDetail.indexOf("On-demand:") !== -1 && data.costDetail.indexOf("fixture") !== -1) {
    cost.hidden = false;
    cost.textContent = data.costDetail;
  }
  var formula = data.formulaCheck;
  if (typeof formula === "string" && formula.indexOf("Formula check only.") === 0 && formula.indexOf("Not the fleet ratio.") !== -1 && formula.length < 180) {
    document.getElementById("formula-check").textContent = formula;
  }
  var reports = data.reports || {};
  var warm = reports.warm;
  var cold = reports.cold;
  if (warm && typeof warm === "object") {
    var card = cardText(warm);
    var line = latencyLine(warm);
    if (card) document.getElementById("workload-card").textContent = card;
    if (line) {
      document.getElementById("quality-line").textContent = line;
      document.getElementById("inference-text").textContent = line;
    }
  }
  if (cold && typeof cold === "object") {
    var coldCard = cardText(cold);
    var coldMs = roundText(cold.timeUntilFirstSuccessMs);
    if (coldCard && coldMs !== null) {
      document.getElementById("cold-line").textContent = "Cold report. " + coldCard + " Time until first success: " + coldMs + " ms.";
    }
  }
}

function applyReady() {
  var count = Number(document.getElementById("ready-count").value);
  return fetch("/api/ready", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ readyReplicas: count }),
  });
}

async function pollLive() {
  var stateResponse = await fetch("/api/state", { cache: "no-store" });
  if (!stateResponse.ok) throw new Error("state");
  var data = await stateResponse.json();
  var seq = ++loadSeq;
  applyLive(data, seq);
  var eventsResponse = await fetch("/api/events", { cache: "no-store" });
  var tracesResponse = await fetch("/api/traces", { cache: "no-store" });
  if (eventsResponse.ok) renderEvents(await eventsResponse.json());
  if (tracesResponse.ok) renderTraces(await tracesResponse.json());
}

function startLive() {
  document.body.dataset.mode = "live";
  document.getElementById("fixture-switch").hidden = true;
  document.getElementById("ready-control").hidden = false;
  document.getElementById("telemetry").hidden = false;
  document.getElementById("footer-note").textContent =
    "Local edge, door, and fake engine on this computer. No cloud account.";
  document.getElementById("apply-ready").addEventListener("click", function () {
    applyReady();
  });
  document.getElementById("ready-count").addEventListener("keydown", function (event) {
    if (event.key === "Enter") applyReady();
  });
  function loop() {
    pollLive().catch(function () {}).then(function () {
      window.setTimeout(loop, 400);
    });
  }
  loop();
}

function boot() {
  var signal = typeof AbortSignal !== "undefined" && AbortSignal.timeout
    ? AbortSignal.timeout(1000)
    : undefined;
  fetch("/api/state", { cache: "no-store", signal: signal }).then(function (response) {
    if (!response.ok) throw new Error("no live state");
    return response.json();
  }).then(function (data) {
    if (!data || typeof data.gpusIdle !== "string" || typeof data.peopleWaiting !== "string") {
      throw new Error("no live state");
    }
    startLive();
  }).catch(function () {
    loadFixture("idle");
  });
}

boot();
