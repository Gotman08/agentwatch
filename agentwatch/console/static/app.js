"use strict";
const $ = (id) => document.getElementById(id),
  esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
const S = {
  csrf: "",
  projects: [],
  providers: [],
  pid: "",
  view: "map",
  epoch: 0,
  summary: null,
  sessions: [],
  reportSession: "",
  reportSection: "overview",
  report: null,
  query: {},
  time: null,
  playIndex: null,
  playTimer: null,
  callCursor: null,
  conversation: "",
  job: "",
  context: null,
  originView: "map",
  scope: {},
  polling: false,
  contextTicket: 0,
  mcpQuery: {},
  mcpPeriod: 0,
  mcpData: null,
  mcpTicket: 0,
  mcpShown: 50,
  mcpLoading: false,
  mcpRevision: null,
  mcpLastUpdated: 0,
  followLive: false,
  liveWindowNs: 3600e9,
  timelineSeconds: 10,
  timelineTicket: 0,
  timelineLoading: false,
  timelineAppliedQuery: null,
};
const labels = {
  success: "Réussi",
  failure: "Échec",
  unknown: "Inconnu",
  open: "Ouvert",
  interrupt: "Interrompu",
  complete: "Terminé",
  failed: "Erreur",
  running: "En cours",
  queued: "En attente",
  cancelled: "Annulé",
  interrupted: "Interrompu",
  error: "Erreur",
  timeout: "Délai dépassé",
  denied: "Refusé",
  codex: "Codex",
  "claude-code": "Claude Code",
  claude: "Claude",
};
const sections = {
  summary: "Synthèse du projet",
  plan: "Plan du projet",
  mcp: "Outils MCP",
  calls: "Appels et durées",
  findings: "Analyses et signalements",
  coverage: "Couverture des mesures",
  agents: "Agents et sous-agents",
  events: "Observations normalisées",
  outputs: "Sorties conservées (sur demande)",
};
const reportSections = [
  ["overview", "Synthèse"],
  ["findings", "Détecteurs"],
  ["stats", "Statistiques"],
  ["agents", "Agents"],
  ["coverage", "Couverture"],
  ["exchanges", "Échanges et contexte"],
  ["graph", "Graphe et remplacements"],
  ["trends", "Tendances"],
  ["compare", "Comparer"],
  ["all", "Rapport complet"],
];
const fmt = (value) => Number(value ?? 0).toLocaleString("fr-FR"),
  ms = (value) =>
    value == null
      ? "Inconnue"
      : value >= 3600000
        ? (value / 3600000).toFixed(1) + " h"
        : value >= 60000
          ? (value / 60000).toFixed(1) + " min"
          : value >= 1000
            ? (value / 1000).toFixed(2) + " s"
            : Math.round(value) + " ms";
const bytes = (value) =>
  value == null
    ? "—"
    : value >= 1048576
      ? (value / 1048576).toFixed(1) + " Mo"
      : value >= 1024
        ? (value / 1024).toFixed(1) + " Ko"
        : value + " o";
const date = (ns) =>
  ns
    ? new Date(Number(ns) / 1e6).toLocaleString("fr-FR", {
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "Non observée";
const clock = (ns) =>
  ns ? new Date(Number(ns) / 1e6).toLocaleTimeString("fr-FR") : "—";
const project = () => S.projects.find((p) => p.id === S.pid),
  base = (pid = S.pid) => "/api/projects/" + encodeURIComponent(pid),
  badge = (value) =>
    `<span class="tag ${/^[a-z-]+$/.test(value || "") ? esc(value) : ""}">${esc(labels[value] || value || "Inconnu")}</span>`;
function notify(message, error = false) {
  $("toast").textContent = message;
  $("toast").className = "toast" + (error ? " error" : "");
  $("toast").hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => ($("toast").hidden = true), 6000);
}
async function api(path, data) {
  const r = await fetch(path, {
    credentials: "same-origin",
    ...(data === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-AgentWatch-CSRF": S.csrf,
          },
          body: JSON.stringify(data),
        }),
  });
  let result;
  try {
    result = await r.json();
  } catch {
    throw Error("Le serveur a retourné une réponse illisible.");
  }
  if (!r.ok) throw Error(result.error || "Erreur " + r.status);
  return result;
}
function empty(title, detail = "") {
  return `<div class="empty"><strong>${esc(title)}</strong>${esc(detail)}</div>`;
}
function header(title, description, actions = "") {
  return `<div class="page-header"><div><span class="eyebrow">${esc(project()?.name || "ESPACE DE TRAVAIL")}</span><h1>${esc(title)}</h1><p class="muted">${esc(description)}</p></div><div class="page-actions">${actions}</div></div>`;
}
function storageLine() {
  return `<p class="storage-line">Historique local · ${esc(project()?.storage || "")} · ${esc(project()?.git || "")}</p>`;
}
function metrics(d) {
  const duration = d.statuses.reduce((n, r) => n + (r.duration_ms || 0), 0),
    unknown = d.statuses.reduce((n, r) => n + (r.unknown || 0), 0);
  return `<div class="metrics">${[
    [
      "Appels observés",
      fmt(d.project.calls),
      fmt(d.project.sessions) + " sessions dans ce dossier",
    ],
    [
      "Serveurs MCP",
      fmt(d.project.mcp_servers),
      fmt(d.mcp.length) + " outils MCP observés",
    ],
    [
      "Temps des outils",
      ms(duration),
      "Somme des durées · inclut le parallélisme",
    ],
    [
      "Échecs observés",
      fmt(d.project.failures),
      fmt(unknown) + " appels sans durée mesurée",
    ],
  ]
    .map(
      (r) =>
        `<div class="metric"><span class="metric-label">${r[0]}</span><div class="metric-value">${r[1]}</div><div class="metric-caption">${r[2]}</div></div>`,
    )
    .join("")}</div>`;
}
function pendingBanner() {
  return project()?.pending_analysis
    ? `<div class="pending-banner">${fmt(project().pending_analysis)} sessions en cours d’analyse. Les traces sont conservées ; les rapports se mettent à jour.</div>`
    : "";
}
function renderProjects() {
  const select = $("project-select");
  select.innerHTML =
    '<option value="">Choisir un projet</option>' +
    S.projects
      .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`)
      .join("");
  select.value = S.pid;
  $("project-list").innerHTML = S.projects
    .map(
      (p) =>
        `<button class="project-card" data-project="${esc(p.id)}"><span><strong>${esc(p.name)}</strong><span class="muted">${esc(p.root)}</span><span class="muted">${fmt(p.events)} observations · ${fmt(p.sessions)} sessions · ${p.map?.running ? "suivi actif" : "démarrage de la carte"}</span></span><span>↗</span></button>`,
    )
    .join("");
}
function chooser() {
  renderProjects();
  $("project-error").textContent = "";
  if (!$("project-dialog").open) $("project-dialog").showModal();
}
function welcome() {
  $("main").innerHTML =
    `<div class="welcome"><span class="eyebrow">VOTRE TRAVAIL, EN PERSPECTIVE</span><h1>Comprendre ce que font<br>vos agents, projet par projet.</h1><p>Une carte vivante, chaque opération dans le temps, et un chat qui s’appuie sur les traces de votre projet.</p><div class="welcome-cards"><div class="welcome-card"><span>⌘</span><h3>Une carte du travail</h3><p>Le plan, les composants et les fichiers qui changent. Le travail déclaré et l’activité observée restent distincts.</p></div><div class="welcome-card"><span>↦</span><h3>Un historique précis</h3><p>Outils, MCP, durées, sorties, erreurs et sous-agents. Rejouez la chronologie au pas de votre choix.</p></div><div class="welcome-card"><span>◌</span><h3>Un contexte maîtrisé</h3><p>Consultez Codex ou Claude. Choisissez les informations jointes et conservez le contexte de chaque réponse.</p></div></div><button id="welcome-open" class="primary">Choisir un dossier de travail <span>↗</span></button></div>`;
  $("welcome-open").onclick = chooser;
}
async function selectProject(pid) {
  stopPlay();
  stopTimelineLive();
  S.pid = pid;
  S.reportSession = "";
  S.conversation = "";
  S.job = "";
  S.context = null;
  S.query = {};
  S.scope = {};
  S.playIndex = null;
  S.time = null;
  S.timelineAppliedQuery = null;
  S.summary = null;
  S.mcpQuery = {};
  S.mcpPeriod = 0;
  S.mcpData = null;
  S.timelineSeconds = 10;
  renderProjects();
  $("project-dialog").close();
  await navigate("map");
}
async function navigate(view) {
  if (view === "chat" && S.view !== "chat") {
    S.originView = S.view;
    if (S.view === "analysis" && S.reportSession)
      S.scope = { session: S.reportSession };
    if (S.view === "mcp") S.scope = { ...(S.mcpData?.selection || S.mcpQuery) };
  }
  stopPlay();
  if (view !== "timeline") stopTimelineLive();
  S.view = view;
  const epoch = ++S.epoch;
  document
    .querySelectorAll("nav button")
    .forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  if (!S.pid) {
    welcome();
    return;
  }
  document.title = "AgentWatch · " + project()?.name;
  $("main").innerHTML = empty("Chargement du projet…");
  try {
    const pid = S.pid;
    if (view === "health") {
      const d = await api("/api/health");
      if (epoch !== S.epoch) return;
      renderHealth(d);
      return;
    }
    const [d, s] = await Promise.all([
      api(base(pid) + "/summary"),
      api(base(pid) + "/sessions"),
    ]);
    if (epoch !== S.epoch) return;
    S.summary = d;
    S.sessions = s;
    if (view === "map") renderMap(d);
    if (view === "mcp") await renderMCP(epoch);
    if (view === "timeline") await renderTimeline(d, epoch);
    if (view === "analysis") await renderAnalysis(epoch);
    if (view === "chat") await renderChat(epoch);
  } catch (e) {
    if (epoch === S.epoch) {
      $("main").innerHTML = empty(
        "Le projet ne peut pas être affiché.",
        e.message,
      );
      notify(e.message, true);
    }
  }
}
function recentCalls(calls) {
  return calls.length
    ? calls
        .map(
          (c) =>
            `<button class="activity-item" data-call="${esc(c.id)}"><span class="activity-meta"><span class="provider">${esc(labels[c.client] || c.client)}${c.agent_id ? " · " + esc(c.agent_id.slice(-8)) : ""}</span><span>${clock(c.order_ns)}</span></span><span class="tool-name">${esc(c.mcp_server ? c.mcp_server + " / " + (c.mcp_tool || c.tool_name) : c.tool_name)}</span><div class="target">${esc(c.label || c.target || "Cible non observée")}</div><span class="activity-meta" style="margin:8px 0 0">${badge(c.status)}<span>${ms(c.duration_reliable === false ? null : c.duration_ms)}</span></span></button>`,
        )
        .join("")
    : empty(
        "Aucun appel observé",
        "Le collecteur alimentera ce projet dès qu’un agent y travaillera.",
      );
}
function mcpList(items) {
  return items.length
    ? items
        .map(
          (r) =>
            `<div class="mcp-row"><div><strong>${esc(r.mcp)}</strong><span class="muted">${esc(r.tool)}</span></div><span class="mcp-total">${fmt(r.n)} appels<br>${ms(r.duration_ms)}</span></div>`,
        )
        .join("")
    : empty(
        "Aucun MCP observé",
        "Les noms apparaîtront lorsqu’un appel MCP sera enregistré.",
      );
}
function renderMap(d) {
  $("main").innerHTML =
    header(
      "Vue du projet",
      "Le plan de travail, les changements de fichiers et les dernières opérations.",
      `<button class="secondary" data-go="timeline">Explorer la timeline ↦</button>`,
    ) +
    pendingBanner() +
    metrics(d) +
    `<div class="map-layout"><section class="panel"><div class="panel-header"><h3>Carte des composants</h3><div class="legend"><span><i></i> Activité observée</span><span><i></i> Statut du plan</span></div></div><iframe class="map-frame" id="map-frame" title="Carte AgentTrail du projet sélectionné" src="/p/${esc(S.pid)}/map" sandbox="allow-scripts allow-same-origin allow-popups"></iframe><div class="panel-foot" id="map-note">Carte issue d’AgentTrail Map, adaptée pour AgentWatch · Les coches du plan n’attestent pas la réussite des opérations.</div></section><aside class="side-stack"><section class="panel"><div class="panel-header"><h3>Dernières opérations</h3><button class="text-button" data-go="timeline">Tout voir</button></div><div class="activity-list" id="recent-calls">${recentCalls(d.recent)}</div></section><section class="panel"><div class="panel-header"><h3>MCP utilisés</h3><button class="text-button" data-go="mcp">Tout voir ↗</button></div><div class="mcp-list" id="map-mcp">${mcpList(d.mcp.slice(0, 6))}</div></section></aside></div>` +
    storageLine();
  updateMapNote();
}
function updateMapNote() {
  const epoch = S.epoch,
    pid = S.pid;
  return api("/p/" + pid + "/model")
    .then((m) => {
      if (epoch !== S.epoch || !$("map-note")) return;
      const parts = [
        "Carte issue d’AgentTrail Map · " +
          (m.hasPlan
            ? "PLAN.md chargé"
            : "Aucun PLAN.md : la carte structurelle reste à renseigner"),
      ];
      if (m.treeTruncated)
        parts.push(
          "Arbre limité à 4 000 fichiers / 8 niveaux : historique des appels conservé séparément",
        );
      if (m.watchMode && m.watchMode !== "recursive")
        parts.push("Suivi des fichiers : " + m.watchMode);
      if (m.changeLogError)
        parts.push("Journal des fichiers : " + m.changeLogError);
      if (m.lints?.length)
        parts.push(m.lints.length + " remarques sur le plan");
      $("map-note").textContent = parts.join(" · ");
    })
    .catch((e) => {
      if (epoch === S.epoch && $("map-note"))
        $("map-note").textContent = e.message.replace(/^RuntimeError: /, "");
    });
}
function localInput(ns) {
  if (!ns) return "";
  const d = new Date(Number(ns) / 1e6);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 19);
}
function timeValue(id) {
  const v = $(id)?.value;
  return v ? Math.round(new Date(v).getTime() * 1e6) : undefined;
}
function queryString(q) {
  return new URLSearchParams(
    Object.entries(q).filter(
      ([, v]) => v !== undefined && v !== null && v !== "",
    ),
  ).toString();
}
function readFilters() {
  return {
    session: $("session-filter")?.value || "",
    client: $("client-filter")?.value || "",
    status: $("status-filter")?.value || "",
    mcp: $("mcp-filter")?.value || "",
    tool: $("tool-filter")?.value || "",
    search: $("search-filter")?.value || "",
    since: timeValue("since-filter"),
    until: timeValue("until-filter"),
  };
}
async function renderTimeline(d, epoch) {
  const choices = S.sessions
    .map(
      (s) =>
        `<option value="${esc(s.id)}">${esc(labels[s.client] || s.client)} · ${date(s.last_ns)} · ${esc(s.native_id.slice(0, 8))}</option>`,
    )
    .join("");
  $("main").innerHTML =
    header(
      "Timeline",
      "Rejouez les opérations. Chaque durée garde sa source et ses limites.",
      `<button class="secondary" id="timeline-live" aria-pressed="false">Suivre en direct</button><button class="secondary" id="timeline-reset">Tout l’historique</button>`,
    ) +
    pendingBanner() +
    `<section class="panel"><div class="panel-header"><h3>Chronologie des outils</h3><span class="muted" style="font-size:10px">Heure locale · ${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)}</span></div><div class="filters"><div class="filter"><label for="since-filter">Début</label><input type="datetime-local" step="1" id="since-filter"></div><div class="filter"><label for="until-filter">Fin</label><input type="datetime-local" step="1" id="until-filter"></div><div class="filter"><label for="step-filter">Un intervalle de</label><select id="step-filter">${AgentWatchTimeline.steps.map((n) => `<option value="${n}" ${n === S.timelineSeconds ? "selected" : ""}>${AgentWatchTimeline.stepLabel(n)}</option>`).join("")}</select></div><button class="secondary" id="timeline-apply">Appliquer</button></div><div class="chart" id="timeline-chart"></div><div class="timeline-controls"><button class="secondary" id="play-toggle">▶ Lire</button><input type="range" id="play-range" min="0" max="0" value="0" aria-label="Position dans la chronologie"><span id="play-time" class="time-readout">Historique complet</span><select id="play-speed" aria-label="Vitesse de lecture"><option value="1">1×</option><option value="2">2×</option><option value="5">5×</option><option value="10">10×</option></select><select id="play-mode" aria-label="Mode de la chronologie"><option value="interval">Cet intervalle</option><option value="cumulative">Cumul depuis le début</option></select><button class="text-button" id="play-all">Tous les appels</button></div><div class="timeline-summary" id="timeline-summary"></div><div class="panel-foot"><span id="timeline-update" role="status"></span><br><span id="timeline-note"></span></div></section><section class="panel" style="margin-top:20px"><div class="filters"><div class="filter"><label for="session-filter">Session</label><select id="session-filter"><option value="">Toutes les sessions</option>${choices}</select></div><div class="filter"><label for="client-filter">Client</label><select id="client-filter"><option value="">Tous les clients</option><option value="codex">Codex</option><option value="claude-code">Claude Code</option></select></div><div class="filter"><label for="status-filter">Résultat</label><select id="status-filter"><option value="">Tous les résultats</option><option value="success">Réussis</option><option value="failure">Échecs</option><option value="open">Ouverts</option><option value="unknown">Inconnus</option></select></div><div class="filter"><label for="mcp-filter">MCP</label><select id="mcp-filter"><option value="">Tous les outils</option>${[...new Set(d.mcp.map((m) => m.mcp))].map((m) => `<option>${esc(m)}</option>`).join("")}</select></div><div class="filter"><label for="tool-filter">Outil MCP</label><select id="tool-filter"><option value="">Tous les outils MCP</option>${[...new Set(d.mcp.map((m) => m.tool))].map((tool) => `<option>${esc(tool)}</option>`).join("")}</select></div><div class="filter search"><label for="search-filter">Rechercher</label><input id="search-filter" placeholder="Outil, fichier, commande, agent…"></div></div><div class="table-wrap"><table><thead><tr><th>Début / fin</th><th>Agent / client</th><th>Outil et cible</th><th>Résultat</th><th>Durée</th><th>Sortie</th></tr></thead><tbody id="calls-body"></tbody></table></div><div class="table-footer"><span id="calls-count"></span><button class="secondary" id="calls-more" hidden>Charger la suite</button></div></section>` +
    storageLine();
  for (const [key, id] of [
    ["session", "session-filter"],
    ["client", "client-filter"],
    ["status", "status-filter"],
    ["mcp", "mcp-filter"],
    ["tool", "tool-filter"],
    ["search", "search-filter"],
  ])
    $(id).value = S.query[key] || "";
  if (S.query.since) $("since-filter").value = localInput(S.query.since);
  if (S.query.until) $("until-filter").value = localInput(S.query.until);
  $("timeline-apply").onclick = () => refreshTimeline();
  $("timeline-live").onclick = toggleTimelineLive;
  $("timeline-reset").onclick = () => {
    stopTimelineLive();
    const since = d.first_ns,
      until = d.last_ns;
    const sec = Math.ceil((until - since) / 1e9 / 700);
    $("step-filter").value =
      [1, 5, 10, 30, 60, 300, 900, 3600, 14400, 86400, 604800].find(
        (n) => n >= sec,
      ) || 3600;
    $("since-filter").value = localInput(since);
    $("until-filter").value = localInput(until);
    refreshTimeline();
  };
  [
    "session-filter",
    "client-filter",
    "status-filter",
    "mcp-filter",
    "tool-filter",
  ].forEach((id) => ($(id).onchange = () => refreshTimeline()));
  let searchTimer;
  $("search-filter").oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => refreshTimeline(), 350);
  };
  $("play-toggle").onclick = togglePlay;
  $("play-mode").onchange = () => {
    drawChart();
    loadCalls();
  };
  $("play-range").oninput = () => {
    stopPlay();
    selectBucket(Number($("play-range").value));
  };
  $("play-all").onclick = () => {
    stopPlay();
    S.playIndex = null;
    drawChart();
    loadCalls();
  };
  $("calls-more").onclick = () => loadCalls(true);
  await refreshTimeline(epoch);
}
async function refreshTimeline(epoch = S.epoch) {
  const ticket = ++S.timelineTicket,
    pid = S.pid;
  stopPlay();
  S.query = readFilters();
  S.timelineSeconds = Number($("step-filter").value);
  if (S.followLive)
    Object.assign(S.query, AgentWatchTimeline.liveRange(S.liveWindowNs));
  const q = { ...S.query };
  S.playIndex = null;
  S.timelineLoading = true;
  try {
    const result = await api(
      base(pid) +
        "/timeline?" +
        queryString({ ...q, seconds: S.timelineSeconds }),
    );
    if (epoch !== S.epoch || ticket !== S.timelineTicket) return;
    S.time = result;
    S.timelineAppliedQuery = {
      ...q,
      since: result.since || q.since,
      until: result.until || q.until,
    };
    S.scope = { ...S.timelineAppliedQuery };
    if (S.followLive || !q.since)
      $("since-filter").value = localInput(result.since);
    if (S.followLive || !q.until)
      $("until-filter").value = localInput(result.until);
    $("play-range").max = Math.max(0, result.buckets.length - 1);
    $("play-range").value = 0;
    $("timeline-note").textContent =
      (result.note || "Aucun appel dans cette sélection.") +
      (result.undated_calls
        ? " " +
          fmt(result.undated_calls) +
          " appels sans horaire conservés, non placés sur la timeline."
        : "");
    $("timeline-update").textContent =
      "Actualisé à " +
      clock(Date.now() * 1e6) +
      (S.followLive
        ? " · fenêtre glissante, aux arrivées du collecteur"
        : " · plage fixe");
    drawChart();
    await loadCalls(false, epoch);
  } catch (e) {
    if (epoch === S.epoch && ticket === S.timelineTicket) {
      stopTimelineLive();
      notify(e.message, true);
    }
  } finally {
    if (ticket === S.timelineTicket) S.timelineLoading = false;
  }
}
function drawChart() {
  const d = S.time;
  if (!$("timeline-chart") || !d) return;
  const bins = d.buckets;
  if (!bins.length) {
    $("timeline-chart").innerHTML = empty("Aucune opération dans cette plage.");
    $("timeline-summary").textContent = "";
    return;
  }
  const width = 1000,
    height = 165,
    step = width / bins.length,
    max = Math.max(1, ...bins.map((b) => b.starts)),
    selected = S.playIndex;
  let svg =
    '<svg viewBox="0 0 1000 180" preserveAspectRatio="none" aria-label="Nombre d’appels commencés par intervalle">';
  for (let i = 0; i < 4; i++) {
    const y = 145 - i * 40;
    svg += `<line x1="0" x2="1000" y1="${y}" y2="${y}"/>`;
  }
  bins.forEach((b, i) => {
    const h = Math.max(2, (b.starts / max) * 130);
    svg += `<rect data-bucket="${i}" x="${i * step + 1}" y="${145 - h}" width="${Math.max(0.7, step - 2)}" height="${h}" class="${b.failures ? "failure " : ""}${selected != null && i > selected ? "future" : ""}"><title>${esc(clock(b.at_ns))} · ${b.starts} appels · actif ${ms(b.active_ms)} · outils ${ms(b.tool_ms)}</title></rect>`;
  });
  for (let i = 0; i < 5; i++) {
    const at = Math.floor(((bins.length - 1) * i) / 4);
    svg += `<text class="axis" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}" x="${i * 250}" y="171">${esc(clock(bins[at].at_ns))}</text>`;
  }
  svg += "</svg>";
  $("timeline-chart").innerHTML = svg;
  const subset =
    selected == null
      ? bins
      : $("play-mode")?.value === "interval"
        ? [bins[selected]]
        : bins.slice(0, selected + 1);
  $("timeline-summary").innerHTML =
    `<span><b>${fmt(subset.reduce((n, b) => n + b.starts, 0))}</b> appels commencés</span><span>Temps actif <b>${ms(subset.reduce((n, b) => n + b.active_ms, 0))}</b></span><span>Temps outils <b>${ms(subset.reduce((n, b) => n + b.tool_ms, 0))}</b></span><span><b>${fmt(subset.reduce((n, b) => n + b.unknown_duration, 0))}</b> durées inconnues</span><span><b>${fmt(subset.reduce((n, b) => n + (b.unplaced_time || 0), 0))}</b> horaires non fiables</span>`;
  $("play-time").textContent =
    selected == null ? "Plage complète" : clock(bins[selected].until_ns);
  $("timeline-chart")
    .querySelectorAll("[data-bucket]")
    .forEach(
      (r) =>
        (r.onclick = () => {
          stopPlay();
          selectBucket(Number(r.dataset.bucket));
        }),
    );
}
function selectBucket(index) {
  stopTimelineLive();
  S.playIndex = index;
  $("play-range").value = index;
  drawChart();
  loadCalls();
}
function togglePlay() {
  if (S.playTimer) {
    stopPlay();
    return;
  }
  if (!S.time?.buckets.length) return;
  stopTimelineLive();
  if (S.playIndex == null || S.playIndex >= S.time.buckets.length - 1)
    S.playIndex = -1;
  $("play-toggle").textContent = "Ⅱ Pause";
  const speed = Number($("play-speed").value);
  S.playTimer = setInterval(
    () => {
      if (S.playIndex >= S.time.buckets.length - 1) {
        stopPlay();
        return;
      }
      selectBucket(S.playIndex + 1);
    },
    Math.max(100, 1000 / speed),
  );
}
function stopPlay() {
  clearInterval(S.playTimer);
  S.playTimer = null;
  if ($("play-toggle")) $("play-toggle").textContent = "▶ Lire";
}
function callRows(calls) {
  return calls
    .map(
      (c) =>
        `<tr data-call="${esc(c.id)}" tabindex="0"><td class="number">${date(c.start_ns || c.end_ns)}<span class="secondary-line">Fin ${clock(c.end_ns)}</span></td><td>${esc(labels[c.client] || c.client)}<span class="secondary-line">${esc(c.agent_id || "Agent principal")}</span></td><td><strong>${esc(c.mcp_server ? c.mcp_server + " / " + (c.mcp_tool || c.tool_name) : c.tool_name || "Non observé")}</strong><span class="secondary-line" title="${esc(c.label || c.target || "")}">${esc(c.label || c.target || "Cible non observée")}</span></td><td>${badge(c.status)}</td><td class="number">${ms(c.duration_reliable === false ? null : c.duration_ms)}<span class="secondary-line">${esc(c.duration_source || "Non mesurée")}</span></td><td class="number">${bytes(c.output_size_bytes)}${c.output_truncated ? '<span class="secondary-line">Tronquée</span>' : ""}</td></tr>`,
    )
    .join("");
}
async function loadCalls(more = false, epoch = S.epoch) {
  const pid = S.pid;
  const q = { ...S.query };
  if (S.time) {
    q.since = q.since || S.time.since;
    q.until = q.until || S.time.until;
  }
  if (S.playIndex != null && S.time?.buckets[S.playIndex]) {
    q.until = S.time.buckets[S.playIndex].until_ns;
    if ($("play-mode")?.value === "interval")
      q.since = S.time.buckets[S.playIndex].at_ns;
  }
  q.overlap = "1";
  if (more && S.callCursor) q.cursor = S.callCursor;
  S.scope = { ...q };
  delete S.scope.cursor;
  const ticket = ++loadCalls.ticket;
  try {
    const d = await api(base(pid) + "/calls?" + queryString(q));
    if (epoch !== S.epoch || ticket !== loadCalls.ticket) return;
    const html = callRows(d.items);
    if (more) $("calls-body").insertAdjacentHTML("beforeend", html);
    else
      $("calls-body").innerHTML =
        html ||
        '<tr><td colspan="6">' +
          empty("Aucun appel dans cette sélection.") +
          "</td></tr>";
    S.callCursor = d.cursor;
    $("calls-more").hidden = !d.cursor;
    $("calls-count").textContent =
      fmt($("calls-body").querySelectorAll("[data-call]").length) +
      " / " +
      fmt(d.total) +
      " appels · cliquer pour les preuves";
  } catch (e) {
    if (epoch === S.epoch) notify(e.message, true);
  }
}
loadCalls.ticket = 0;
function structured(value, depth = 0) {
  if (value === null || value === undefined)
    return '<span class="muted">Non observé</span>';
  if (typeof value !== "object")
    return esc(typeof value === "boolean" ? (value ? "Oui" : "Non") : value);
  const entries = Array.isArray(value)
    ? value.map((v, i) => [String(i + 1), v])
    : Object.entries(value);
  if (!entries.length)
    return '<span class="muted">Aucune donnée dans cette section.</span>';
  if (depth > 7) return `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`;
  return (
    entries
      .slice(0, 120)
      .map(([key, v]) =>
        v !== null && typeof v === "object"
          ? `<details ${depth === 0 ? "open" : ""}><summary>${esc(key.replaceAll("_", " "))} <span class="badge-count">${Array.isArray(v) ? v.length : Object.keys(v).length}</span></summary><div>${structured(v, depth + 1)}</div></details>`
          : `<div class="field"><span class="field-name">${esc(key.replaceAll("_", " "))}</span><span class="field-value">${structured(v, depth + 1)}</span></div>`,
      )
      .join("") +
    (entries.length > 120
      ? '<p class="muted">Affichage limité aux 120 premiers éléments ; l’export JSON conserve le rapport complet.</p>'
      : "")
  );
}
async function showCall(id) {
  stopTimelineLive();
  stopPlay();
  const pid = S.pid;
  try {
    const d = await api(base(pid) + "/calls/" + encodeURIComponent(id));
    if (pid !== S.pid) return;
    const c = d.call;
    $("detail-dialog").querySelector("h2").textContent = "Détail de l’appel";
    $("detail-content").innerHTML =
      `<div class="detail-heading">${badge(c.status)}<h3>${esc(c.mcp_server ? c.mcp_server + " / " + (c.mcp_tool || c.tool_name) : c.tool_name)}</h3></div><p class="muted" style="font-size:12px">${esc(c.label || c.target || "Cible non observée")}</p><div class="detail-meta">${[
        ["Durée", ms(c.duration_reliable === false ? null : c.duration_ms)],
        ["Source de la durée", c.duration_source || "Non mesurée"],
        ["Taille de la sortie", bytes(c.output_size_bytes)],
        [
          "Client / modèle",
          (labels[c.client] || c.client) + " · " + (c.model || "Non observé"),
        ],
        ["Début", date(c.start_ns)],
        ["Fin", date(c.end_ns)],
      ]
        .map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`)
        .join(
          "",
        )}</div><div class="report-note">${esc(d.output_note)}${c.output_truncated ? " Sortie signalée comme tronquée." : ""}</div><section class="panel" style="margin-bottom:20px"><div class="panel-header"><h3>Sortie native de cet appel</h3><button class="secondary" id="capture-output">Lire et conserver la sortie</button></div><pre id="native-output" style="margin:0;border:0">${esc(d.output?.text || "La sortie peut être relue dans le journal natif, puis conservée ici, secrets masqués. Aucun outil n’est exécuté.")}</pre><div class="panel-foot" id="native-output-note">${d.output ? (d.output.complete ? "Sortie entière conservée" : "Extrait tronqué conservé") + " · " + esc(d.output.source?.file || "") : "Extrait limité à 20 000 caractères. Ajout au contexte du chat sur votre choix."}</div></section><div class="json-tree">${structured({ parametres: c.params, erreur: c.error_summary, code_de_sortie: c.exit_code, preuves: c.evidence, avertissements: c.warnings, observations: d.events, appel_complet: c })}</div>`;
    $("capture-output").onclick = async () => {
      const b = $("capture-output");
      b.disabled = true;
      try {
        const o = await api(base(pid) + "/source", {
          call: id,
          limit_chars: 20000,
        });
        if (pid !== S.pid) return;
        $("native-output").textContent = o.text;
        $("native-output-note").textContent =
          (o.complete
            ? "Sortie entière conservée"
            : "Extrait tronqué conservé") +
          " · " +
          o.source.file;
        notify("Sortie masquée conservée dans ce projet.");
      } catch (err) {
        $("native-output-note").textContent = err.message;
      } finally {
        b.disabled = false;
      }
    };
    $("detail-dialog").showModal();
  } catch (e) {
    notify(e.message, true);
  }
}
async function renderAnalysis(epoch) {
  if (!S.sessions.length) {
    $("main").innerHTML =
      header(
        "Analyses",
        "Les détecteurs AgentWatch, leurs preuves et la couverture des données.",
      ) +
      empty(
        "Pas encore de session",
        "Les analyses apparaîtront après l’import des observations de ce projet.",
      );
    return;
  }
  if (!S.sessions.some((s) => s.id === S.reportSession))
    S.reportSession = S.sessions[0].id;
  const session = S.sessions.find((s) => s.id === S.reportSession);
  $("main").innerHTML =
    header(
      "Analyses",
      "Les rapports AgentWatch existants, avec leurs preuves et leurs limites.",
      `<select id="report-session" aria-label="Session analysée">${S.sessions.map((s) => `<option value="${esc(s.id)}">${esc(labels[s.client] || s.client)} · ${date(s.last_ns)} · ${esc(s.native_id.slice(0, 8))}</option>`).join("")}</select><button class="secondary" id="reanalyse">Recalculer</button>`,
    ) +
    `<div class="analysis-layout"><aside class="panel analysis-sections">${reportSections.map(([id, name]) => `<button data-section="${id}" class="${S.reportSection === id ? "active" : ""}">${name}<span>›</span></button>`).join("")}</aside><section class="panel"><div class="panel-header"><h3 id="report-title">Rapport de session</h3><span><a id="export-json" class="text-button" href="${base()}/report/${S.reportSession}?format=json" download>JSON ↓</a><a id="export-md" class="text-button" href="${base()}/report/${S.reportSession}?format=md" download>Markdown ↓</a></span></div><div class="section-content" id="report-content">${empty("Analyse en cours…")}</div></section></div>` +
    storageLine();
  $("report-session").value = S.reportSession;
  $("report-session").onchange = async () => {
    S.reportSession = $("report-session").value;
    await renderAnalysis(S.epoch);
  };
  $("reanalyse").onclick = async () => {
    await api(base() + "/analyse", { session: S.reportSession });
    notify("Recalcul demandé pour cette session.");
    await loadReport(S.epoch);
  };
  document.querySelectorAll("[data-section]").forEach(
    (b) =>
      (b.onclick = () => {
        S.reportSection = b.dataset.section;
        renderReport();
      }),
  );
  await loadReport(epoch);
}
async function loadReport(epoch) {
  const sid = S.reportSession;
  const d = await api(base() + "/report/" + sid);
  if (epoch !== S.epoch || sid !== S.reportSession) return;
  S.report = d;
  renderReport();
}
function findingCards(findings) {
  return findings.length
    ? findings
        .map(
          (f) =>
            `<article class="finding"><div class="finding-tags"><span class="tag">${esc(f.detector || f.rule || "Signalement")}</span><span class="tag">Confiance ${esc(f.confidence || "non définie")}</span>${f.feedback ? '<span class="tag">' + esc(f.feedback.mark) + "</span>" : ""}</div><h3>${esc(f.title || f.summary || f.description || f.finding_id)}</h3>${f.explanation ? "<p>" + esc(f.explanation) + "</p>" : ""}<div class="json-tree">${structured(f)}</div><div class="finding-actions"><button class="secondary" data-feedback="relevant" data-finding="${esc(f.finding_id || "")}">Utile</button><button class="text-button" data-feedback="false-positive" data-finding="${esc(f.finding_id || "")}">Faux positif</button><button class="text-button" data-feedback="clear" data-finding="${esc(f.finding_id || "")}">Effacer l’avis</button></div></article>`,
        )
        .join("")
    : empty(
        "Aucun signalement dans ce rapport",
        "Cela décrit les observations disponibles et les règles activées ; cela ne prouve pas l’absence de problème.",
      );
}
function renderReport() {
  if (["trends", "compare"].includes(S.reportSection)) {
    renderHistoricalSection();
    return;
  }
  const result = S.report;
  if (!$("report-content") || !result) return;
  $("export-json").hidden = false;
  $("export-md").hidden = false;
  const r = result.report;
  document
    .querySelectorAll("[data-section]")
    .forEach((b) =>
      b.classList.toggle("active", b.dataset.section === S.reportSection),
    );
  $("report-title").textContent =
    reportSections.find(([id]) => id === S.reportSection)?.[1] || "Rapport";
  let html = result.pending
    ? '<div class="pending-banner">De nouvelles observations sont en cours d’analyse. Le rapport affiché correspond à la révision ' +
      result.analysed_revision +
      ".</div>"
    : "";
  if (result.error) html += '<p class="error">' + esc(result.error) + "</p>";
  if (!r.session) {
    $("report-content").innerHTML =
      html + empty("Le rapport n’est pas encore disponible.");
    return;
  }
  if (S.reportSection === "overview")
    html += `<div class="report-note">${esc(r.project_scope?.note || "")} ${r.project_scope?.truncated ? "Analyse limitée : " + fmt(r.project_scope.events_analysed) + " / " + fmt(r.project_scope.events_retained) + " événements. Toutes les traces restent conservées." : ""}</div><div class="json-tree">${structured({ session: r.session, provenance: r.provenance, classement: r.ranking_criteria, cout_union: r.finding_cost_union, absence_signalement: r.no_issue_statement, perimetre: r.project_scope })}</div>`;
  else if (S.reportSection === "findings")
    html += findingCards(r.findings || []);
  else if (S.reportSection === "exchanges")
    html +=
      '<div class="json-tree">' +
      structured(
        Object.fromEntries(
          Object.entries(r.stats || {}).filter(([k]) =>
            /context|exchange|delivery|usage|token|work/.test(k),
          ),
        ),
      ) +
      "</div>";
  else if (S.reportSection === "graph")
    html +=
      '<div class="json-tree">' +
      structured({
        graphe_observe: r.observed_dependency_graph,
        remplacements: (r.findings || []).map((f) => ({
          id: f.finding_id,
          replacement: f.replacement || f.replacements || null,
        })),
      }) +
      "</div>";
  else
    html +=
      '<div class="json-tree">' +
      structured(S.reportSection === "all" ? r : r[S.reportSection]) +
      "</div>";
  $("report-content").innerHTML = html;
}
async function renderChat(epoch) {
  const pid = S.pid,
    conversations = await api(base(pid) + "/conversations");
  if (epoch !== S.epoch) return;
  $("main").innerHTML =
    header(
      "Chat du projet",
      "Consultez Codex ou Claude avec un contexte sélectionné et conservé.",
    ) +
    `<div class="chat-layout"><section class="panel conversation-panel"><div class="chat-toolbar"><select id="chat-provider" aria-label="Fournisseur du chat">${S.providers.map((p) => `<option value="${esc(p.id)}" ${p.installed ? "" : "disabled"}>${esc(p.name)}${p.installed ? "" : " · non installé"}</option>`).join("")}</select><input id="chat-model" placeholder="Modèle du CLI (par défaut)" aria-label="Modèle facultatif"><select id="conversation-select" aria-label="Conversation du projet"><option value="">Nouvelle conversation</option>${conversations.map((c) => `<option value="${esc(c.id)}">${esc(c.title)}</option>`).join("")}</select><button class="text-button" id="new-chat">＋ Nouveau</button></div><div class="messages" id="messages"></div><form class="composer" id="chat-form"><textarea id="chat-message" aria-label="Message à Codex ou Claude" placeholder="Que s’est-il passé ? Quels MCP ont été utilisés ? Où avons-nous perdu du temps ?" required></textarea><div class="composer-actions"><span class="muted">Consultation · le contexte choisi est joint au message</span><span><button type="button" class="secondary" id="chat-cancel" hidden>Annuler</button><button type="submit" class="primary" id="chat-send" disabled>Envoyer ↑</button></span></div><p id="chat-error" class="error"></p></form></section><aside class="panel context-panel"><div class="panel-header"><h3>Contexte transmis</h3><span class="tag">Ce projet</span></div><div class="context-options"><div class="context-root">${esc(project().root)}</div>${Object.entries(
      sections,
    )
      .map(
        ([id, name]) =>
          `<label class="check"><input type="checkbox" name="context-section" value="${id}" ${["summary", "plan", "mcp", "calls", "findings", "coverage"].includes(id) ? "checked" : ""}>${name}</label>`,
      )
      .join(
        "",
      )}<label class="check"><input type="checkbox" id="context-scope" ${Object.keys(S.scope).length ? "checked" : ""}>Utiliser le périmètre de la vue précédente</label><div class="input-group"><div><label for="context-limit">Appels récents (max.)</label><input type="number" id="context-limit" min="1" max="200" value="100"></div><div><label for="context-budget">Budget en caractères</label><input type="number" id="context-budget" min="8000" max="240000" step="8000" value="80000"></div></div><button id="context-prepare" class="secondary" style="margin-top:17px;width:100%">Préparer / actualiser le contexte</button></div><div class="context-summary" id="context-summary">Le contexte sera visible ici avant l’envoi.</div><details><summary style="padding:12px 20px;font-size:11px;cursor:pointer">Aperçu exact du contexte joint</summary><pre class="context-preview" id="context-preview"></pre></details></aside></div>` +
    storageLine();
  $("conversation-select").value = S.conversation;
  $("conversation-select").onchange = () => {
    S.conversation = $("conversation-select").value;
    S.job = "";
    loadMessages();
  };
  $("new-chat").onclick = () => {
    S.conversation = "";
    S.job = "";
    $("conversation-select").value = "";
    renderMessages([]);
  };
  $("context-prepare").onclick = () => prepareContext();
  document
    .querySelectorAll(
      "input[name=context-section],#context-scope,#context-limit,#context-budget",
    )
    .forEach((e) => {
      const invalidate = () => {
        S.contextTicket++;
        S.context = null;
        $("chat-send").disabled = true;
        $("context-prepare").disabled = false;
        $("context-preview").textContent =
          "Préparez le nouveau contexte pour afficher cette sélection.";
        $("context-summary").textContent =
          "Sélection modifiée. Préparez le nouveau contexte.";
      };
      e.oninput = invalidate;
      e.onchange = invalidate;
    });
  $("chat-form").onsubmit = sendChat;
  $("chat-cancel").onclick = async () => {
    if (S.job) {
      await api(base() + "/cancel", { message: S.job });
      notify("Annulation demandée.");
    }
  };
  $("chat-message").onkeydown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      $("chat-form").requestSubmit();
    }
  };
  await loadMessages(epoch);
  await prepareContext(epoch);
}
async function prepareContext(epoch = S.epoch) {
  const ticket = ++S.contextTicket,
    pid = S.pid;
  S.context = null;
  $("chat-send").disabled = true;
  const options = {
    sections: [
      ...document.querySelectorAll("input[name=context-section]:checked"),
    ].map((e) => e.value),
    page: S.originView,
    call_limit: Number($("context-limit").value),
    budget_chars: Number($("context-budget").value),
    ...($("context-scope").checked ? S.scope : {}),
  };
  try {
    $("context-prepare").disabled = true;
    const d = await api(base(pid) + "/context", options);
    if (epoch !== S.epoch || ticket !== S.contextTicket) return;
    S.context = d;
    $("context-preview").textContent = JSON.stringify(d.bundle, null, 2);
    $("context-summary").innerHTML =
      `<b>${fmt(d.chars)}</b> caractères · ~${fmt(d.estimated_tokens)} tokens estimés<br>${Object.keys(d.bundle.sections).length} sections · ${d.bundle.omissions.length ? esc(d.bundle.omissions.map((o) => (o.items ? o.section + " : " + o.items + " éléments écartés" : o.section + " omise")).join(", ")) : "aucune section omise"}<br>Appels limités à ${options.call_limit}. Synthèse : historique du projet. MCP : ${esc(d.bundle.sections.mcp?.scope || "section non jointe")}.<br>Empreinte <code>${esc(d.digest.slice(0, 12))}</code>`;
    $("chat-send").disabled = Boolean(S.job);
    $("context-prepare").disabled = false;
  } catch (e) {
    if (epoch === S.epoch && ticket === S.contextTicket) {
      $("context-summary").textContent = e.message;
      $("context-prepare").disabled = false;
      notify(e.message, true);
    }
  }
}
async function sendChat(e) {
  e.preventDefault();
  if (!S.context) return notify("Préparez le contexte avant l’envoi.", true);
  const message = $("chat-message").value.trim();
  if (!message || S.job) return;
  const pid = S.pid,
    epoch = S.epoch,
    data = {
      provider: $("chat-provider").value,
      model: $("chat-model").value.trim(),
      message,
      context_id: S.context.id,
      conversation: S.conversation || null,
    };
  try {
    $("chat-send").disabled = true;
    $("chat-error").textContent = "";
    const result = await api(base(pid) + "/chat", data);
    if (epoch !== S.epoch) return;
    S.conversation = result.conversation;
    S.job = result.message;
    $("chat-message").value = "";
    const list = await api(base(pid) + "/conversations");
    $("conversation-select").innerHTML =
      '<option value="">Nouvelle conversation</option>' +
      list
        .map((c) => `<option value="${esc(c.id)}">${esc(c.title)}</option>`)
        .join("");
    $("conversation-select").value = S.conversation;
    await loadMessages(epoch);
  } catch (err) {
    if (epoch === S.epoch) {
      $("chat-error").textContent = err.message;
      $("chat-send").disabled = false;
    }
  }
}
async function loadMessages(epoch = S.epoch) {
  if (!$("messages")) return;
  if (!S.conversation) {
    renderMessages([]);
    return;
  }
  const cid = S.conversation;
  const messages = await api(
    base() + "/conversation/" + encodeURIComponent(cid),
  );
  if (epoch !== S.epoch || cid !== S.conversation) return;
  const running = messages.find((m) =>
    ["running", "queued"].includes(m.status),
  );
  S.job = running?.id || "";
  renderMessages(messages);
  $("chat-cancel").hidden = !S.job;
  $("chat-send").disabled = Boolean(S.job) || !S.context;
}
function renderMessages(messages) {
  const box = $("messages");
  if (!box) return;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.innerHTML = messages.length
    ? messages
        .map(
          (m) =>
            `<article class="message ${esc(m.role)}"><div class="message-meta"><strong>${m.role === "user" ? "Vous" : esc(labels[m.provider] || m.provider)}</strong><span>${date(m.created_ns)}</span>${m.role === "assistant" ? badge(m.status) : ""}${m.digest ? '<span title="Empreinte du contexte joint">contexte ' + esc(m.digest.slice(0, 8)) + "</span>" + (m.role === "user" ? '<button class="text-button" data-message-context="' + esc(m.id) + '">Voir le contexte</button>' : "") : ""}</div><div class="message-content">${m.role === "assistant" ? (m.content === m.error ? "" : renderChatText(m.content)) : esc(m.content)}</div>${["queued", "running"].includes(m.status) ? '<p class="typing">Le modèle consulte le contexte du projet…</p>' : ""}${m.error ? '<p class="error">' + esc(m.error) + "</p>" : ""}</article>`,
        )
        .join("")
    : empty(
        "Une conversation ancrée dans votre projet",
        "Choisissez le modèle et le contexte, puis posez votre question.",
      );
  if (nearBottom || !messages.length) box.scrollTop = box.scrollHeight;
}
function renderHealth(d) {
  $("main").innerHTML =
    header(
      "Collecte et suivi",
      "L’état réel de la collecte, de la lecture des traces et des projets suivis.",
    ) +
    `<div class="health-grid"><section class="panel"><div class="panel-header"><h3>Collecteur AgentWatch</h3>${badge(d.collector.running ? "running" : "unknown")}</div><div class="panel-body"><div class="json-tree">${structured(d.collector)}</div><p class="fineprint">${esc(d.note)}</p></div></section><section class="panel"><div class="panel-header"><h3>Lecture vers les projets</h3><span class="tag">${esc(d.reader.state)}</span></div><div class="panel-body"><div class="json-tree">${structured(d.reader)}</div></div></section><section class="panel" style="grid-column:1/-1"><div class="panel-header"><h3>Projets suivis en parallèle</h3><button class="text-button" id="health-add">Ajouter un dossier</button></div><div class="panel-body">${d.projects.map((p) => `<div class="health-item"><span><b>${esc(p.name)}</b><br>${esc(p.root)}</span><span>${fmt(p.events)} observations · ${fmt(p.calls)} appels · ${p.map.running ? "carte active" : esc(p.map.error || "démarrage")} <button class="text-button" data-unfollow="${esc(p.id)}">Arrêter ce suivi</button></span></div>`).join("")}<div class="json-tree">${structured({ erreurs_registry: d.registry_errors, erreur: d.error })}</div></div></section></div>`;
  $("health-add").onclick = chooser;
}
async function browse(path) {
  try {
    const d = await api(
      "/api/folders?" +
        queryString({ path: path || $("project-path").value || undefined }),
    );
    $("project-path").value = d.path;
    $("folder-browser").hidden = false;
    $("folder-browser").innerHTML =
      `<span class="folder-path">${esc(d.path)}</span><button data-folder="${esc(d.parent)}">↰ Dossier parent</button>${d.folders.map((n) => `<button data-folder="${esc(d.path + (d.path.includes("\\") ? "\\" : "/") + n)}">▱ ${esc(n)}</button>`).join("")}${d.truncated ? '<span class="folder-path">Les 200 premiers dossiers sont affichés.</span>' : ""}`;
    $("folder-browser")
      .querySelectorAll("[data-folder]")
      .forEach((b) => (b.onclick = () => browse(b.dataset.folder)));
  } catch (e) {
    $("project-error").textContent = e.message;
  }
}
async function poll() {
  if (S.polling || document.hidden) return;
  S.polling = true;
  try {
    const d = await api("/api/bootstrap");
    S.projects = d.projects;
    const r = d.reader;
    const pending = S.projects.reduce((n, p) => n + p.pending_analysis, 0);
    $("live-label").textContent =
      r.state === "backfill"
        ? "Import de l’historique · " + fmt(r.read) + " observations lues"
        : pending
          ? "Analyse de " + fmt(pending) + " sessions"
          : fmt(S.projects.length) + " projets suivis";
    document.querySelector(".live-dot").className =
      "live-dot" +
      (r.state === "error"
        ? " error"
        : r.state === "backfill" || pending
          ? " busy"
          : "");
    renderProjects();
    const pid = S.pid,
      epoch = S.epoch;
    if (S.pid && S.view === "map") {
      const summary = await api(base(pid) + "/summary");
      if (epoch === S.epoch) {
        S.summary = summary;
        const current = $("recent-calls");
        if (current) current.innerHTML = recentCalls(summary.recent);
        const cards = document.querySelector(".metrics");
        if (cards) cards.outerHTML = metrics(summary);
        const banner = document.querySelector(".pending-banner");
        if (banner && !summary.project.pending_analysis) banner.remove();
        if ($("map-mcp"))
          $("map-mcp").innerHTML = mcpList(summary.mcp.slice(0, 6));
        await updateMapNote();
      }
    }
    if (S.view === "chat" && S.conversation) await loadMessages(epoch);
    if (
      S.view === "timeline" &&
      S.followLive &&
      !S.timelineLoading &&
      !$("detail-dialog").open
    )
      await refreshTimeline(epoch);
    if (
      S.view === "mcp" &&
      !S.mcpLoading &&
      (S.mcpRevision !== project()?.analysis_revision ||
        Date.now() - S.mcpLastUpdated > 60000)
    )
      await refreshMCP(epoch, true);
    if (S.view === "analysis" && S.report?.pending) await loadReport(epoch);
  } catch (e) {
    $("live-label").textContent = "Connexion interrompue";
    document.querySelector(".live-dot").className = "live-dot error";
  } finally {
    S.polling = false;
  }
}
document
  .querySelectorAll("nav button")
  .forEach((b) => (b.onclick = () => navigate(b.dataset.view)));
$("project-select").onchange = () => selectProject($("project-select").value);
$("home").onclick = chooser;
$("add-project").onclick = chooser;
$("close-project").onclick = () => $("project-dialog").close();
$("close-detail").onclick = () => $("detail-dialog").close();
$("health-button").onclick = () => navigate("health");
$("browse-project").onclick = () => browse();
$("project-form").onsubmit = async (e) => {
  e.preventDefault();
  const btn = $("project-form").querySelector("[type=submit]");
  btn.disabled = true;
  try {
    const p = await api("/api/projects", {
      root: $("project-path").value.trim(),
    });
    S.projects = await api("/api/projects");
    await selectProject(p.id);
    notify("Projet ouvert. Son suivi reste actif en parallèle.");
  } catch (err) {
    $("project-error").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};
$("project-list").onclick = (e) => {
  const b = e.target.closest("[data-project]");
  if (b) selectProject(b.dataset.project);
};
$("main").onclick = async (e) => {
  const oldContext = e.target.closest("[data-message-context]");
  if (oldContext) {
    const pid = S.pid;
    try {
      const d = await api(
        base(pid) + "/message-context/" + oldContext.dataset.messageContext,
      );
      if (pid !== S.pid) return;
      $("detail-dialog").querySelector("h2").textContent =
        "Contexte de ce message";
      $("detail-content").innerHTML =
        '<p class="report-note">Empreinte ' +
        esc(d.digest) +
        " · " +
        date(d.created_ns) +
        "</p><pre>" +
        esc(JSON.stringify(d.bundle, null, 2)) +
        "</pre>";
      $("detail-dialog").showModal();
    } catch (err) {
      notify(err.message, true);
    }
    return;
  }
  const unfollow = e.target.closest("[data-unfollow]");
  if (unfollow) {
    try {
      await api(base(unfollow.dataset.unfollow) + "/unfollow", {});
      S.projects = await api("/api/projects");
      if (S.pid === unfollow.dataset.unfollow) S.pid = "";
      renderProjects();
      notify("Suivi arrêté. L’historique reste dans le dossier du projet.");
      navigate(S.pid ? "health" : "map");
    } catch (err) {
      notify(err.message, true);
    }
    return;
  }
  const call = e.target.closest("[data-call]");
  if (call) {
    showCall(call.dataset.call);
    return;
  }
  const go = e.target.closest("[data-go]");
  if (go) {
    navigate(go.dataset.go);
    return;
  }
  const feedback = e.target.closest("[data-feedback]");
  if (feedback) {
    try {
      await api(base() + "/feedback", {
        id: feedback.dataset.finding,
        mark: feedback.dataset.feedback,
      });
      notify("Avis conservé dans ce projet.");
    } catch (err) {
      notify(err.message, true);
    }
  }
};
$("main").onkeydown = (e) => {
  if (e.key === "Enter" && e.target.matches("[data-call]"))
    showCall(e.target.dataset.call);
};
async function boot() {
  try {
    const d = await api("/api/bootstrap");
    Object.assign(S, {
      csrf: d.csrf,
      projects: d.projects,
      providers: d.providers,
    });
    renderProjects();
    welcome();
    $("live-label").textContent = fmt(S.projects.length) + " projets suivis";
    setInterval(poll, 3000);
    if (S.projects.length) chooser();
  } catch (e) {
    $("main").innerHTML = empty("La console ne répond pas.", e.message);
  }
}
boot();
