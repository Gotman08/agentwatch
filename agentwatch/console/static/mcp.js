"use strict";

async function renderMCP(epoch) {
  const sessions = S.sessions
    .map(
      (s) =>
        `<option value="${esc(s.id)}">${esc(labels[s.client] || s.client)} · ${date(s.last_ns)} · ${esc(s.native_id.slice(0, 8))}</option>`,
    )
    .join("");
  const servers = [...new Set(S.summary.mcp.map((r) => r.mcp))];
  $("main").innerHTML =
    header(
      "MCP",
      "Les serveurs et outils observés, leurs performances et les preuves de chaque appel.",
      '<a class="secondary" id="mcp-export">JSON ↓</a>',
    ) +
    pendingBanner() +
    `<section class="panel"><div class="filters">
      <div class="filter"><label for="mcp-period">Période MCP</label><select id="mcp-period"><option value="0">Tout l’historique</option><option value="3600">Dernière heure</option><option value="86400">24 dernières heures</option><option value="604800">7 derniers jours</option><option value="2592000">30 derniers jours</option></select></div>
      <div class="filter"><label for="mcp-session">Session MCP</label><select id="mcp-session"><option value="">Toutes les sessions</option>${sessions}</select></div>
      <div class="filter"><label for="mcp-client">Client MCP</label><select id="mcp-client"><option value="">Tous les clients</option><option value="codex">Codex</option><option value="claude-code">Claude Code</option></select></div>
      <div class="filter"><label for="mcp-server">Serveur MCP</label><select id="mcp-server"><option value="">Tous les serveurs</option>${servers.map((name) => `<option>${esc(name)}</option>`).join("")}</select></div>
      <div class="filter"><label for="mcp-result">Résultat MCP</label><select id="mcp-result"><option value="">Tous les résultats</option><option value="success">Réussis</option><option value="failure">Échecs</option><option value="open">Ouverts</option><option value="unknown">Inconnus</option></select></div>
      <div class="filter search"><label for="mcp-search">Rechercher un outil MCP</label><input id="mcp-search" placeholder="Outil, fichier, commande…"></div>
    </div><div class="panel-foot" id="mcp-update" role="status"></div></section>
    <div id="mcp-metrics" style="margin-top:20px"></div><div class="mcp-servers" id="mcp-servers"></div>
    <section class="panel"><div class="panel-header"><h3>Outils observés</h3><select id="mcp-sort" aria-label="Trier les outils MCP"><option value="calls">Nombre d’appels</option><option value="p95_ms">Latence P95</option><option value="failures">Échecs</option><option value="output_bytes">Volume de sortie</option></select></div>
    <div class="table-wrap"><table><thead><tr><th>Serveur / outil</th><th>Appels</th><th>Résultats</th><th>Médiane / P95</th><th>Temps outils</th><th>Sortie observée</th><th>Dernier appel</th><th>Preuves</th></tr></thead><tbody id="mcp-tools"></tbody></table></div>
    <div class="table-footer"><span id="mcp-count"></span><button class="secondary" id="mcp-more" hidden>Afficher plus d’outils</button></div><div class="panel-foot">Les durées et tailles absentes restent inconnues. P95 : seuil couvrant au moins 95 % des durées mesurées. Les durées sont celles enregistrées par le client, erreurs incluses ; le temps outils inclut le parallélisme. Un serveur observé n’atteste pas une connexion actuelle.</div></section>` +
    storageLine();
  for (const [key, id] of [
    ["session", "mcp-session"],
    ["client", "mcp-client"],
    ["mcp", "mcp-server"],
    ["status", "mcp-result"],
    ["search", "mcp-search"],
  ])
    $(id).value = S.mcpQuery[key] || "";
  $("mcp-period").value = S.mcpPeriod;
  for (const id of [
    "mcp-session",
    "mcp-client",
    "mcp-server",
    "mcp-result",
    "mcp-period",
  ])
    $(id).onchange = () => refreshMCP();
  let searchTimer;
  $("mcp-search").oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => refreshMCP(), 350);
  };
  $("mcp-sort").onchange = () => {
    S.mcpShown = 50;
    drawMCPTools();
  };
  $("mcp-more").onclick = () => {
    S.mcpShown += 50;
    drawMCPTools();
  };
  await refreshMCP(epoch);
}

async function refreshMCP(epoch = S.epoch, keepPage = false) {
  if (S.view !== "mcp" || !$("mcp-period")) return;
  const ticket = ++S.mcpTicket,
    pid = S.pid,
    revision = project()?.analysis_revision;
  S.mcpPeriod = Number($("mcp-period").value);
  const q = {
    session: $("mcp-session").value,
    client: $("mcp-client").value,
    mcp: $("mcp-server").value,
    status: $("mcp-result").value,
    search: $("mcp-search").value,
  };
  if (S.mcpPeriod) {
    q.until = Date.now() * 1e6;
    q.since = q.until - S.mcpPeriod * 1e9;
  }
  S.mcpLoading = true;
  $("mcp-update").textContent = "Actualisation de la sélection…";
  try {
    const data = await api(base(pid) + "/mcp?" + queryString(q));
    if (epoch !== S.epoch || ticket !== S.mcpTicket) return;
    S.mcpQuery = q;
    S.mcpData = data;
    if (!keepPage) S.mcpShown = 50;
    S.mcpLastUpdated = Date.now();
    S.mcpRevision = revision;
    const serverSelect = $("mcp-server");
    for (const row of data.servers) {
      if (![...serverSelect.options].some((option) => option.value === row.mcp))
        serverSelect.insertAdjacentHTML(
          "beforeend",
          `<option>${esc(row.mcp)}</option>`,
        );
    }
    S.scope = { ...q };
    const t = data.total;
    $("mcp-metrics").innerHTML = `<div class="metrics">${[
      [
        "Appels MCP",
        fmt(t.calls),
        `${fmt(data.servers.length)} serveurs · ${fmt(data.tools.length)} outils`,
      ],
      [
        "Latence médiane",
        ms(t.median_ms),
        `${fmt(t.measured_calls)} mesurés · ${fmt(t.unknown_duration_calls)} durées inconnues`,
      ],
      [
        "Latence P95",
        ms(t.p95_ms),
        "95 % des mesures à ce seuil ou en dessous",
      ],
      [
        "Échecs observés",
        fmt(t.failures),
        `${mcpRate(t.failure_rate_pct)} sur résultats connus · ${fmt(t.open_calls)} ouverts`,
      ],
    ]
      .map(
        (r) =>
          `<div class="metric"><span class="metric-label">${r[0]}</span><div class="metric-value">${r[1]}</div><div class="metric-caption">${r[2]}</div></div>`,
      )
      .join("")}</div>`;
    $("mcp-servers").innerHTML =
      data.servers
        .map(
          (r) =>
            `<article class="panel mcp-server-card"><div class="panel-header"><h3>${esc(r.mcp)}</h3><button class="text-button" data-server-proof="${esc(r.mcp)}" aria-label="Voir les appels de ${esc(r.mcp)}">Appels ↗</button></div><div class="mcp-server-body"><strong>${fmt(r.calls)} <span>appels</span></strong><div class="mcp-server-values"><span>Médiane <b>${ms(r.median_ms)}</b></span><span>P95 <b>${ms(r.p95_ms)}</b></span><span>Temps outils <b>${ms(r.total_ms)}</b></span><span>Sortie <b>${bytes(r.output_bytes)}</b></span></div><div class="mcp-server-states">${badge("success")} ${fmt(r.successes)} ${badge("failure")} ${fmt(r.failures)} ${badge("open")} ${fmt(r.open_calls)}</div><p class="muted">${fmt(r.unknown_duration_calls)} durées inconnues · ${fmt(r.unknown_output_calls)} sorties sans taille · ${fmt(r.truncated_calls)} tronquées · ${fmt(r.undated_calls)} sans horaire</p></div></article>`,
        )
        .join("") || empty("Aucun serveur MCP dans cette sélection.");
    $("mcp-update").textContent =
      `Actualisé à ${clock(Date.now() * 1e6)} · ${fmt(project()?.pending_analysis)} sessions en attente d’analyse · Cette racine uniquement`;
    $("mcp-export").href =
      base(pid) + "/mcp?" + queryString({ ...q, format: "json" });
    drawMCPTools();
    $("mcp-servers")
      .querySelectorAll("[data-server-proof]")
      .forEach(
        (button) =>
          (button.onclick = () => inspectMCP(button.dataset.serverProof)),
      );
  } catch (e) {
    if (epoch === S.epoch && ticket === S.mcpTicket) {
      $("mcp-update").textContent = "Actualisation impossible : " + e.message;
      notify(e.message, true);
    }
  } finally {
    if (ticket === S.mcpTicket) S.mcpLoading = false;
  }
}

function mcpRate(value) {
  return value == null
    ? "Taux inconnu"
    : value.toLocaleString("fr-FR", { maximumFractionDigits: 1 }) + " %";
}

function drawMCPTools() {
  if (!S.mcpData || !$("mcp-tools")) return;
  const sort = $("mcp-sort").value;
  const tools = [...S.mcpData.tools].sort(
    (a, b) =>
      (b[sort] ?? -1) - (a[sort] ?? -1) ||
      String(a.tool).localeCompare(String(b.tool)),
  );
  const shown = tools.slice(0, S.mcpShown);
  $("mcp-tools").innerHTML =
    shown
      .map(
        (r) =>
          `<tr><td><strong>${esc(r.mcp)}</strong><span class="secondary-line">${esc(r.tool || "Outil non observé")}</span></td><td class="number">${fmt(r.calls)}<span class="secondary-line">${fmt(r.sessions)} sessions</span></td><td>${fmt(r.successes)} réussis · ${fmt(r.failures)} échecs<span class="secondary-line">${fmt(r.open_calls)} ouverts · ${fmt(r.other_status_calls)} autres états</span></td><td class="number" title="Médiane ${esc(r.median_ms)} ms ; P95 ${esc(r.p95_ms)} ms">${ms(r.median_ms)} / ${ms(r.p95_ms)}<span class="secondary-line">${fmt(r.measured_calls)} mesurés · ${fmt(r.unknown_duration_calls)} inconnus</span></td><td class="number">${ms(r.total_ms)}</td><td class="number">${bytes(r.output_bytes)}<span class="secondary-line">${fmt(r.unknown_output_calls)} inconnues · ${fmt(r.truncated_calls)} tronquées</span></td><td class="number">${date(r.last_ns)}</td><td><button class="text-button" data-mcp-proof="${esc(r.mcp)}" data-tool-proof="${esc(r.tool || "")}" aria-label="Voir les appels de ${esc(r.tool || r.mcp)}">Appels ↗</button></td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="8">' +
      empty("Aucun outil MCP dans cette sélection.") +
      "</td></tr>";
  $("mcp-count").textContent =
    `${fmt(shown.length)} / ${fmt(tools.length)} outils · données de la sélection`;
  $("mcp-more").hidden = shown.length >= tools.length;
  $("mcp-tools")
    .querySelectorAll("[data-mcp-proof]")
    .forEach(
      (button) =>
        (button.onclick = () =>
          inspectMCP(button.dataset.mcpProof, button.dataset.toolProof)),
    );
}

async function inspectMCP(server, tool = "") {
  if (S.mcpLoading) {
    notify("La sélection est en cours d’actualisation.");
    return;
  }
  const q = { ...S.mcpQuery, mcp: server, tool };
  const record = tool
    ? S.mcpData.tools.find((row) => row.mcp === server && row.tool === tool)
    : S.mcpData.servers.find((row) => row.mcp === server);
  q.since ||= Math.floor((record?.first_ns || S.summary.first_ns) / 1e9) * 1e9;
  q.until ||=
    Math.ceil((record?.last_ns || S.summary.last_ns) / 1e9) * 1e9 + 1e9;
  if (!Number.isFinite(q.since) || !q.since) delete q.since;
  if (!Number.isFinite(q.until) || !q.until) delete q.until;
  S.query = q;
  S.timelineSeconds =
    q.since && q.until ? AgentWatchTimeline.stepForRange(q.since, q.until) : 10;
  await navigate("timeline");
}
