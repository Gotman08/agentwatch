"use strict";
function renderHistoricalSection() {
  const kind = S.reportSection === "trends" ? "trends" : "compare",
    title =
      kind === "trends" ? "Tendances du projet" : "Comparer des observations";
  document
    .querySelectorAll("[data-section]")
    .forEach((b) =>
      b.classList.toggle("active", b.dataset.section === S.reportSection),
    );
  $("report-title").textContent = title;
  $("export-json").hidden = true;
  $("export-md").hidden = true;
  const sessionOptions = S.sessions
    .map(
      (s) =>
        `<option value="${esc(s.id)}">${esc(labels[s.client] || s.client)} · ${date(s.last_ns)} · ${esc(s.native_id.slice(0, 8))}</option>`,
    )
    .join("");
  const controls =
    kind === "trends"
      ? `<div class="filter"><label for="history-days">Période</label><select id="history-days"><option value="7">7 jours</option><option value="30">30 jours</option><option value="90">90 jours</option><option value="0">Tout l’historique</option></select></div><div class="filter"><label for="history-limit">Sessions récentes (max.)</label><input id="history-limit" type="number" value="100" min="1" max="1000"></div>`
      : `<div class="filter"><label for="compare-before">Session de référence</label><select id="compare-before">${sessionOptions}</select></div><div class="filter"><label for="compare-mode">Comparaison</label><select id="compare-mode"><option value="sessions">Deux sessions</option><option value="window">Avant / après une date</option></select></div><div class="filter" id="compare-after-box"><label for="compare-after">Session suivante</label><select id="compare-after">${sessionOptions}</select></div><div class="filter" id="compare-boundary-box" hidden><label for="compare-boundary">Séparer cette session à</label><input type="datetime-local" step="1" id="compare-boundary"></div>`;
  $("report-content").innerHTML =
    `<div class="report-note">${kind === "trends" ? "Les sept détecteurs AgentWatch sont appliqués aux sessions de ce dossier. Les motifs récurrents gardent leurs preuves et le retour utilisateur." : "La comparaison utilise les mesures d’AgentWatch et les observations choisies. Elle ne prouve pas un gain causal."}</div><div class="filters">${controls}<button class="primary" id="history-run">Calculer</button></div><div id="history-result">${empty("Choisissez le périmètre, puis lancez le calcul.", "Le résultat est conservé dans .agentwatch/reports de ce projet.")}</div>`;
  if (kind === "compare") {
    $("compare-before").value = S.reportSession;
    $("compare-after").value =
      S.sessions.find((s) => s.id !== S.reportSession)?.id || S.reportSession;
    $("compare-mode").onchange = () => {
      const windowMode = $("compare-mode").value === "window";
      $("compare-after-box").hidden = windowMode;
      $("compare-boundary-box").hidden = !windowMode;
      const session = S.sessions.find(
        (s) => s.id === $("compare-before").value,
      );
      if (session)
        $("compare-boundary").value = localInput(
          (session.first_ns + session.last_ns) / 2,
        );
    };
  }
  $("history-run").onclick = async () => {
    const epoch = S.epoch,
      pid = S.pid,
      section = S.reportSection;
    const options =
      kind === "trends"
        ? {
            days: Number($("history-days").value),
            session_limit: Number($("history-limit").value),
          }
        : {
            before: $("compare-before").value,
            ...($("compare-mode").value === "window"
              ? { boundary_ns: timeValue("compare-boundary") }
              : { after: $("compare-after").value }),
          };
    try {
      $("history-run").disabled = true;
      $("history-result").innerHTML = empty(
        "Calcul en cours…",
        "Les autres vues et le suivi des projets restent disponibles.",
      );
      let job = await api(base(pid) + "/" + kind, options);
      while (job.status === "running") {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        if (epoch !== S.epoch || section !== S.reportSection) return;
        job = await api(base(pid) + "/history/" + job.id);
      }
      if (epoch !== S.epoch || section !== S.reportSection) return;
      if (job.error) throw Error(job.error);
      $("export-json").href = base(pid) + "/history/" + job.id + "?format=json";
      $("export-json").hidden = false;
      $("history-result").innerHTML =
        `<div class="panel-foot">Résultat conservé · ${esc(job.id)}</div><div class="json-tree" style="padding-top:18px">${structured(job.result)}</div>`;
    } catch (e) {
      if (epoch === S.epoch && section === S.reportSection) {
        $("history-result").innerHTML = empty("Calcul indisponible", e.message);
        notify(e.message, true);
      }
    } finally {
      if (epoch === S.epoch && section === S.reportSection)
        $("history-run").disabled = false;
    }
  };
  const pid = S.pid,
    epoch = S.epoch,
    section = S.reportSection;
  api(base(pid) + "/history")
    .then((items) => {
      if (epoch !== S.epoch || section !== S.reportSection) return;
      const saved = items.filter((item) => item.kind === kind);
      if (!saved.length) return;
      const picker = document.createElement("div");
      picker.className = "filters";
      picker.innerHTML = `<div class="filter"><label for="saved-history">Résultats conservés (100 plus récents)</label><select id="saved-history"><option value="">Choisir un résultat</option>${saved.map((item) => `<option value="${esc(item.id)}">${esc(date(item.created_ns))} · ${esc(item.id.slice(0, 8))}</option>`).join("")}</select></div>`;
      $("history-result").before(picker);
      $("saved-history").onchange = async () => {
        const key = $("saved-history").value;
        if (!key) return;
        try {
          const job = await api(base(pid) + "/history/" + key);
          if (epoch !== S.epoch || section !== S.reportSection) return;
          $("export-json").href =
            base(pid) + "/history/" + key + "?format=json";
          $("export-json").hidden = false;
          $("history-result").innerHTML =
            `<div class="panel-foot">Résultat conservé · ${esc(key)}</div><div class="json-tree" style="padding-top:18px">${structured(job.result)}</div>`;
        } catch (e) {
          notify(e.message, true);
        }
      };
    })
    .catch((e) => {
      if (epoch === S.epoch && section === S.reportSection)
        notify(e.message, true);
    });
}
