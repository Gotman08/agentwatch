"use strict";

const AgentWatchTimeline = {
  steps: [1, 5, 10, 30, 60, 300, 900, 3600, 14400, 86400, 604800],
  stepLabel(seconds) {
    const [n, unit] =
      seconds < 60
        ? [seconds, "seconde"]
        : seconds < 3600
          ? [seconds / 60, "minute"]
          : seconds < 86400
            ? [seconds / 3600, "heure"]
            : [seconds / 86400, "jour"];
    return n + " " + unit + (n > 1 ? "s" : "");
  },
  stepForRange(since, until) {
    return (
      this.steps.find((n) => n >= Math.ceil((until - since) / 1e9 / 700)) ||
      604800
    );
  },
  liveRange(span, now = Date.now()) {
    const until = Math.floor(now / 1000) * 1e9;
    return { since: until - Math.max(1e9, span), until };
  },
};
if (typeof module !== "undefined") module.exports = AgentWatchTimeline;

function stopTimelineLive() {
  if (S.followLive) {
    S.timelineTicket++;
    S.timelineLoading = false;
    if (typeof loadCalls === "function") loadCalls.ticket++;
    if (S.timelineAppliedQuery) {
      S.query = { ...S.timelineAppliedQuery };
      for (const [key, id] of [
        ["session", "session-filter"],
        ["client", "client-filter"],
        ["status", "status-filter"],
        ["mcp", "mcp-filter"],
        ["tool", "tool-filter"],
        ["search", "search-filter"],
      ])
        if ($(id)) $(id).value = S.query[key] || "";
      if ($("since-filter"))
        $("since-filter").value = localInput(S.query.since);
      if ($("until-filter"))
        $("until-filter").value = localInput(S.query.until);
    }
  }
  S.followLive = false;
  if ($("timeline-live")) {
    $("timeline-live").setAttribute("aria-pressed", "false");
    $("timeline-live").textContent = "Suivre en direct";
    $("since-filter").readOnly = $("until-filter").readOnly = false;
  }
}

async function toggleTimelineLive() {
  if (S.followLive) {
    stopTimelineLive();
    return;
  }
  stopPlay();
  S.followLive = true;
  S.liveWindowNs = Math.max(
    S.time?.seconds * 1e9 || 1e9,
    S.time?.until - S.time?.since || 3600e9,
  );
  $("timeline-live").setAttribute("aria-pressed", "true");
  $("timeline-live").textContent = "● Suivi en direct";
  $("since-filter").readOnly = $("until-filter").readOnly = true;
  await refreshTimeline();
}
