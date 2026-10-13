const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const timeline = require("../agentwatch/console/static/timeline.js");
const source = fs.readFileSync(
  path.join(__dirname, "../agentwatch/console/static/app.js"),
  "utf8",
);
const refresh = source.slice(
  source.indexOf("async function refreshTimeline("),
  source.indexOf("function drawChart("),
);

function harness() {
  const fields = {};
  for (const id of [
    "since-filter",
    "until-filter",
    "step-filter",
    "play-range",
    "timeline-note",
    "timeline-update",
  ])
    fields[id] = { value: "", textContent: "" };
  fields["step-filter"].value = "10";
  const pending = [],
    loaded = [],
    filters = { tool: "first" };
  const S = { pid: "root-a", epoch: 1, timelineTicket: 0, followLive: false };
  const sandbox = {
    S,
    $: (id) => fields[id],
    readFilters: () => ({ ...filters }),
    stopPlay: () => {},
    stopTimelineLive: () => {
      S.followLive = false;
    },
    AgentWatchTimeline: {
      liveRange: (span) => timeline.liveRange(span, 1791806400000),
    },
    base: (pid) => "/api/projects/" + pid,
    queryString: (q) => new URLSearchParams(q).toString(),
    api: (url) => new Promise((resolve) => pending.push({ url, resolve })),
    drawChart: () => {},
    loadCalls: async () => loaded.push(S.time),
    localInput: (ns) => String(ns),
    clock: () => "",
    notify: (message) => {
      throw new Error(message);
    },
    Date,
  };
  vm.createContext(sandbox);
  vm.runInContext(refresh, sandbox);
  return {
    sandbox,
    S,
    fields,
    pending,
    loaded,
    filters,
    run: () => sandbox.refreshTimeline(),
  };
}

test("an older timeline response cannot overwrite newer filters or load their calls", async () => {
  const h = harness();
  const old = h.run();
  h.filters.tool = "second";
  const recent = h.run();
  const newer = { since: 20, until: 40, buckets: [], marker: "second" };
  h.pending[1].resolve(newer);
  await recent;
  h.pending[0].resolve({ since: 1, until: 10, buckets: [], marker: "first" });
  await old;
  assert.equal(h.S.time.marker, "second");
  assert.equal(h.loaded.length, 1);
  assert.equal(h.S.query.tool, "second");
  assert.equal(h.S.timelineLoading, false);
});

test("live following slides the window while preserving the selected MCP tool", async () => {
  const h = harness();
  h.S.followLive = true;
  h.S.liveWindowNs = 3600e9;
  h.S.scope = { tool: "frozen", since: 100, until: 200 };
  h.S.timelineAppliedQuery = { ...h.S.scope };
  const request = h.run();
  const url = new URL(h.pending[0].url, "http://localhost");
  assert.equal(url.searchParams.get("tool"), "first");
  assert.equal(
    Number(url.searchParams.get("until")) -
      Number(url.searchParams.get("since")),
    3600e9,
  );
  const end = Number(url.searchParams.get("until"));
  h.pending[0].resolve({ since: end - 3600e9, until: end, buckets: [] });
  await request;
  assert.equal(h.S.scope.tool, "first");
  assert.equal(h.S.scope.until, end);
});

test("a project switch rejects the response of the previous root", async () => {
  const h = harness();
  const request = h.run();
  h.S.pid = "root-b";
  h.S.epoch++;
  h.pending[0].resolve({ since: 1, until: 2, buckets: [] });
  await request;
  assert.equal(h.S.time, undefined);
  assert.equal(h.loaded.length, 0);
  assert.equal(h.S.scope, undefined);
});

test("the step fits the requested history without altering its range", () => {
  const step = timeline.stepForRange(0, 30 * 86400e9);
  assert.ok((30 * 86400) / step <= 720);
  assert.equal(timeline.stepLabel(1), "1 seconde");
  assert.equal(timeline.stepLabel(60), "1 minute");
  const one = timeline.liveRange(3600e9, 1791806400123),
    two = timeline.liveRange(3600e9, 1791806406123);
  assert.equal(two.since - one.since, 6e9);
  assert.equal(two.until - two.since, 3600e9);
});

test("stopping live follow freezes the view even when its request finishes later", async () => {
  const h = harness();
  h.fields["timeline-live"] = { setAttribute() {}, textContent: "" };
  const helper = fs.readFileSync(
    path.join(__dirname, "../agentwatch/console/static/timeline.js"),
    "utf8",
  );
  vm.runInContext(
    helper.slice(
      helper.indexOf("function stopTimelineLive("),
      helper.indexOf("async function toggleTimelineLive("),
    ),
    h.sandbox,
  );
  h.S.followLive = true;
  h.S.liveWindowNs = 3600e9;
  h.S.scope = { tool: "frozen", since: 100, until: 200 };
  h.S.timelineAppliedQuery = { ...h.S.scope };
  const request = h.run();
  h.sandbox.stopTimelineLive();
  h.pending[0].resolve({ since: 1, until: 2, buckets: [] });
  await request;
  assert.equal(h.S.followLive, false);
  assert.equal(h.S.timelineLoading, false);
  assert.equal(h.S.time, undefined);
  assert.equal(h.loaded.length, 0);
  assert.equal(h.S.scope.tool, "frozen");
  assert.equal(h.S.query.tool, "frozen");
});
