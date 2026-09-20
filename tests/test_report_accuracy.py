"""Exactitude des rapports et des classifications (defauts releves le 2026-09-20 sur une session Codex reelle).

Un test par defaut corrige : totaux du tableau des outils, reconciliation des agents, identifiants non ambigus,
provenance (rollout et non hooks), tracabilite des cibles MCP, erreurs structurees, conclusions qui ne presentent
plus une sequence normale de developpement comme une economie, et separation des natures de tokens.

# * Rollouts SYNTHETIQUES, ecrits d'apres les formes relevees sur des rollouts reels (noms de champs et valeurs
#   categorielles seulement). Aucun rollout reel de la machine n'est lu.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agentwatch import cli
from agentwatch.collector import rollouts as R
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core import normalize as N
from agentwatch.core.correlate import ORIGIN_ROLLOUT, SessionView
from agentwatch.core.session import load_session
from agentwatch.detectors import run_detectors
from agentwatch.reports.json_report import build_report
from agentwatch.reports.labels import agent_labels, unique_prefixes
from agentwatch.reports.markdown import render_markdown
from agentwatch.reports.stats import compute_stats, coverage_matrix
from tests.test_rollouts import ROOT, RolloutBuilder

# * Identifiants de fil Codex : horodates, donc deux sous-agents lances a quelques secondes d'ecart partagent
#   leurs 9 premiers caracteres (c'est le cas reel qui rendait deux lignes indiscernables).
CHILD_A = "01a0bf98-91ee-75c2-8f78-a5350a18957a"
CHILD_B = "01a0bf98-d0cd-7f62-a6a6-ba0a60fbc475"


def _item(b: RolloutBuilder, it: dict[str, Any]) -> None:
    start = b.ms()
    b.add("event_msg", {"type": "item_completed", "thread_id": b.thread_id, "turn_id": "t", "item": it,
                        "started_at_ms": start, "completed_at_ms": start + 20})


class ReportAccuracyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.sessions = root / "home", root / "sessions"
        self.day = self.sessions / "2026" / "09" / "20"
        self.day.mkdir(parents=True)
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"health": {"codex_sessions_dir": str(self.sessions)},
                                                           "rollouts": {"background_priority": False, "auto_import": False}}),
                                               encoding="utf-8")
        self.cfg = load_config(self.home)
        self.store = EventStore(self.home, self.cfg)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------ session synthetique
    def _write(self, b: RolloutBuilder) -> Path:
        p = self.day / f"rollout-2026-09-20T18-00-00-{b.thread_id}.jsonl"
        p.write_text(b.text(), encoding="utf-8", newline="\n")
        return p

    def _session(self) -> SessionView:
        """Fil principal + deux sous-agents, avec ce qui a fait echouer les rapports : fiches MCP differentes,
        erreurs de formes variees, cycle lire/modifier/tester, arret d'un sous-agent suivi d'un nouvel appel."""
        main = RolloutBuilder(ROOT).meta().turn("turn-1", "Programme du jour.")
        # deux fiches Linear differentes, lues chacune deux fois : la cible affichee doit les distinguer
        for issue in ("NYK-100", "NYK-200"):
            for _ in range(2):
                main.fn(f"mcp_{issue}_{_}", None, "mcp__linear__get_issue", {"id": issue}, f"contenu de {issue}")
        # erreurs de formes differentes : PowerShell, rg, script qui ecrit son propre code, sortie non classable
        main.exec_("e_ps", [main.cmd("i_ps", "Get-Content -LiteralPath 'Source/x.cpp'",
                                     "du code\r\nGet-Content: \r\nLine |\r\n   2 |  Get-Content -Litera\r\n"
                                     "     |  ~~~~~~~~\r\n     | Cannot find path 'Source/x.cpp' because it does not exist.\r\n", code=1)])
        main.exec_("e_rg", [main.cmd("i_rg", "rg motif Source/*.cpp",
                                     "rg: Source/*.cpp: The system cannot find the file specified. (os error 123)\n", code=1)])
        main.exec_("e_sc", [main.cmd("i_sc", "& './build.ps1'",
                                     '{\r\n  "StartedUTC": "2026-09-20T16:39:56Z",\r\n  "ExitCode": 6,\r\n  "Target": "Editor"\r\n}\r\n',
                                     code=1)])
        main.exec_("e_un", [main.cmd("i_un", "Get-Content -LiteralPath 'Source/y.cpp' -TotalCount 5",
                                     "ligne de code sans marqueur d'erreur\r\nautre ligne\r\n", code=1)])
        main.end_turn("turn-1")
        # * Cycle normal de developpement : lire, modifier (contenu different a chaque fois), relancer le test. Chaque
        #   etape est un exec distinct, donc une reponse distincte du modele (un aller-retour entre deux etapes).
        for i in range(3):
            main.exec_(f"c_read_{i}", [main.cmd(f"i_read_{i}", f"Get-Content -LiteralPath 'Scripts/mod_{i}.py'", f"source {i}")])
            main.exec_(f"c_edit_{i}", [{"type": "FileChange", "id": f"i_edit_{i}", "status": "completed",
                                        "changes": {f"Scripts/mod_{i}.py": {"type": "update",
                                                                            "unified_diff": f"@@\n-ancien {i}\n+nouveau {i}\n"}}}])
            main.exec_(f"c_test_{i}", [main.cmd(f"i_test_{i}", f"python -m pytest Scripts/mod_{i}.py", f"1 passed {i}")])
        children = {}
        for child, nick in ((CHILD_A, "Gauss"), (CHILD_B, "Nietzsche")):
            b = RolloutBuilder(child, parent=ROOT, nickname=nick).meta().turn("turn-c")
            b.exec_(f"x_{child[-4:]}", [b.cmd(f"ix_{child[-4:]}", "python -m unittest discover", "ok")])
            children[child] = b
        # * Gauss : un arret « termine » PUIS un nouvel appel -> sa fin n'est pas etablie (il a repris).
        _item(main, {"type": "SubAgentActivity", "id": "sa_stop", "kind": "completed", "agent_thread_id": CHILD_A,
                     "agent_path": "/root/tache"})
        gauss = children[CHILD_A]
        gauss.t = main.t                                       # * l'appel qui suit est bien posterieur a l'arret
        gauss.exec_("x_again", [gauss.cmd("ix_again", "python -m unittest discover", "ok")])
        self._write(main)
        for b in children.values():
            self._write(b)
        R.import_rollouts(self.store, self.cfg, [str(x) for x in sorted(self.day.glob("*.jsonl"))])
        client, skey = cli._resolve_session(self.store, ROOT, None)
        return load_session(self.store, client, skey, self.cfg)

    def _report(self, view: SessionView) -> dict[str, Any]:
        return build_report(view, compute_stats(view, self.cfg), coverage_matrix(view),
                            run_detectors(view, self.cfg), self.cfg, {}, None)

    # ------------------------------------------------------------------ coherence des donnees affichees
    def test_tool_table_totals_match_calls(self) -> None:
        """Le tableau des outils, limite a 15 lignes, retombe sur le nombre d'appels annonce."""
        view = self._session()
        st = compute_stats(view, self.cfg)
        self.assertEqual(st["tools_total"]["calls"], st["calls"])
        self.assertEqual(sum(t["calls"] for t in st["tools"]), st["calls"])
        md = render_markdown(self._report(view))
        self.assertIn(f"**Total : {st['tools_total']['tools']} outil(s)** | **{st['calls']}**", md)

    def test_agents_reconciled_with_known_facts(self) -> None:
        """Surnom, fil parent, modele et tokens du fil sont repris ; la fin non etablie le reste."""
        view = self._session()
        by_id = {a.agent_id: a for a in view.agent_infos}
        self.assertEqual({a.nickname for a in by_id.values()}, {"Gauss", "Nietzsche"})
        for a in by_id.values():
            self.assertEqual(a.parent_agent_id, "main")
            self.assertIsNotNone(a.parent_basis)
            self.assertEqual(a.model, "gpt-synth")           # turn_context du fil, pas celui de la session
            self.assertIsNotNone(a.model_basis)
            self.assertIsNotNone(a.usage)
            self.assertGreater((a.usage or {})["total_tokens"], 0)
            self.assertEqual(a.origin, ORIGIN_ROLLOUT)
        # * Gauss est repris apres son arret : sa fin n'est pas etablie, et rien ne l'invente.
        self.assertIsNone(by_id[CHILD_A].stop_time)
        self.assertEqual(by_id[CHILD_A].end_state, "resumed_after_stop")
        self.assertIsNotNone(by_id[CHILD_A].last_stop_time)
        md = render_markdown(self._report(view))
        self.assertIn("inconnue : repris apres son dernier arret observe", md)
        self.assertNotIn("| en cours |", md)

    def test_agent_ids_are_unambiguous(self) -> None:
        """Deux identifiants au meme debut recoivent des etiquettes distinctes."""
        pre = unique_prefixes([CHILD_A, CHILD_B])
        self.assertNotEqual(pre[CHILD_A], pre[CHILD_B])
        labels = agent_labels(["main", CHILD_A, CHILD_B],
                              [{"agent_id": CHILD_A, "nickname": "Gauss"}, {"agent_id": CHILD_B, "nickname": "Nietzsche"}])
        self.assertEqual(labels["main"], "principal")
        self.assertIn("Gauss", labels[CHILD_A])
        self.assertNotEqual(labels[CHILD_A], labels[CHILD_B])
        md = render_markdown(self._report(self._session()))
        self.assertIn(f"`{CHILD_A}`", md)                    # identifiant complet dans le tableau des agents

    # ------------------------------------------------------------------ provenance
    def test_no_hook_wording_for_rollout_session(self) -> None:
        """Une session lue dans les rollouts ne cite aucun hook : ni base de couverture, ni surcout, ni duree."""
        view = self._session()
        self.assertEqual(view.collection, "rollout")
        md = render_markdown(self._report(view))
        self.assertIn("rollouts Codex lus passivement", md)
        self.assertIn("Surcharge des hooks : sans objet", md)
        self.assertNotIn("PreToolUse", md)
        self.assertNotIn("PostToolUse", md)
        self.assertNotIn("reconstruite entre hooks", md)
        self.assertNotIn("du PATH lors de `configure`", md)
        for row in coverage_matrix(view):
            self.assertEqual(row["source"], "rollout")
            self.assertNotIn("hook", row["basis"].lower())

    def test_hook_wording_kept_for_claude_code(self) -> None:
        """Les libelles des hooks restent pour une session Claude Code reellement collectee par hooks."""
        from agentwatch import CLIENT_CLAUDE_CODE
        from agentwatch.selftest import Synth
        s = Synth(self.home, client=CLIENT_CLAUDE_CODE, cfg=self.cfg)
        s.session_start()
        s.call("Read", {"file_path": "a.py"}, {"content": "x"})
        view = load_session(self.store, *cli._resolve_session(self.store, s.session_id, None), self.cfg)
        self.assertEqual(view.collection, "hooks")
        rows = coverage_matrix(view)
        self.assertTrue(any("PreToolUse" in r["basis"] for r in rows))
        self.assertTrue(all(r["source"] == "hooks" for r in rows))

    # ------------------------------------------------------------------ tracabilite
    def test_mcp_targets_show_issue_ids(self) -> None:
        """Les fiches Linear sont distinguees a l'affichage, et n'etaient deja pas fusionnees au regroupement."""
        view = self._session()
        calls = [c for c in view.calls if c.tool_name == "mcp__linear__get_issue"]
        self.assertEqual(len(calls), 4)
        self.assertEqual({c.target for c in calls}, {"mcp:linear/get_issue"})       # cible brute : identique
        self.assertEqual({c.label for c in calls},                                   # cible affichee : distincte
                         {"mcp:linear/get_issue id=NYK-100", "mcp:linear/get_issue id=NYK-200"})
        # * Le regroupement n'a jamais melange les fiches : deux cles d'unite de travail distinctes.
        self.assertEqual(len({c.op_key for c in calls}), 2)
        units = [w for w in compute_stats(view, self.cfg)["work_units"] if w["op"] == "mcp_read"]
        self.assertEqual({w["target"] for w in units},
                         {"mcp:linear/get_issue id=NYK-100", "mcp:linear/get_issue id=NYK-200"})
        md = render_markdown(self._report(view))
        self.assertIn("id=NYK-100", md)
        self.assertIn("id=NYK-200", md)

    def test_errors_are_structured_and_sourced(self) -> None:
        """Nature de l'erreur, statut de l'outil, code de sortie et resultat du script sont distingues et sources."""
        view = self._session()
        err = compute_stats(view, self.cfg)["errors"]
        kinds = {g["kind"]: g for g in err["groups"]}
        self.assertIn("powershell", kinds)
        self.assertIn("rg", kinds)
        self.assertIn("script_result", kinds)
        self.assertIn("unclassified", kinds)                 # rien n'est devine
        ps = kinds["powershell"]
        self.assertIn("Cannot find path", ps["detail"])
        self.assertEqual(ps["exit_code"], 1)
        self.assertIn("rollout", ps["exit_code_basis"])
        self.assertEqual(ps["tool_status"], "failed")
        self.assertTrue(ps["sources"] and ps["sources"][0]["file"].endswith(".jsonl"))
        self.assertIsNone(ps["script_exit_code"])
        self.assertEqual(kinds["script_result"]["script_exit_code"], 6)              # code ecrit par le script
        self.assertEqual(kinds["script_result"]["exit_code"], 1)                     # code de la commande
        self.assertIsNone(kinds["unclassified"]["detail"])
        md = render_markdown(self._report(view))
        self.assertIn("Erreurs : statut de l'outil, code de sortie, resultat du script", md)
        self.assertNotIn("Signatures d'erreur", md)

    def test_error_classification_never_guesses(self) -> None:
        """Un fragment de code n'est pas une erreur classee ; chaque forme reconnue l'est sur sa vraie ligne."""
        self.assertEqual(N.classify_error("Vector2D(X, Y));\r\n\t\t}\r\n")["kind"], "unclassified")
        self.assertEqual(N.classify_error("{")["kind"], "unclassified")
        self.assertEqual(N.classify_error(None)["kind"], "unclassified")
        py = N.classify_error('Traceback (most recent call last):\n  File "x.py", line 3\nAssertionError: 3 != 4\n')
        self.assertEqual(py["kind"], "python")
        self.assertIn("AssertionError", py["detail"])
        svc = N.classify_error('{"error":"upstream_unavailable","message":"indisponible","status":503}')
        self.assertEqual(svc["kind"], "service")
        self.assertEqual(svc["detail"], "upstream_unavailable")
        # deux chemins differents, meme nature : regroupes
        a = N.classify_error("rg: Source/a.cpp: The system cannot find the file specified. (os error 123)")
        b = N.classify_error("rg: Source/b.cpp: The system cannot find the file specified. (os error 123)")
        self.assertEqual(a["detail"], b["detail"])

    # ------------------------------------------------------------------ conclusions
    def test_dev_cycle_is_not_a_demonstrated_saving(self) -> None:
        """Lire, modifier, tester : signale sans etre une opportunite, et sans annoncer d'economie."""
        view = self._session()
        report = self._report(view)
        cycles = [f for f in report["findings"]
                  if f["rule_id"] == "D.automation_candidates" and f["evidence"].get("development_cycle")]
        self.assertTrue(cycles, "le cycle lire/modifier/tester doit etre reconnu")
        for f in cycles:
            self.assertEqual(f["confidence"], "low")
            self.assertEqual(f["evidence"]["avoidable_round_trips"], 0)
            self.assertFalse(f["evidence"]["saving_demonstrated"])
            self.assertGreater(f["evidence"]["round_trips_between_steps"], 0)
            self.assertIn("Cycle normal de developpement", f["explanation"])
            self.assertNotIn(f["finding_id"], report["top_findings"])

    def test_unknown_patch_content_is_judgment(self) -> None:
        """Un contenu d'edition inconnu n'est jamais suppose identique (donc jamais mecanique)."""
        from agentwatch.detectors.automation_candidates import _analyse
        from agentwatch.core.correlate import Call
        from agentwatch.core import schema as S

        def edit(fp: str | None) -> Call:
            c = Call(key=f"k{fp}", client="codex", session_id="s", tool_name="apply_patch", category=S.CAT_EDIT,
                     target="Scripts/a.py", status=S.STATUS_SUCCESS)
            c.params = {"patch_fp": fp}
            return c
        info = _analyse(("edit",), [[edit(None)], [edit(None)], [edit(None)]])
        self.assertEqual(info["judgment_steps"], 1)
        self.assertIn("inconnu", info["steps"][0]["reason"])

    def test_wait_separates_requested_and_observed(self) -> None:
        """Delai demande a l'outil et intervalle observe entre deux appels ne sont pas confondus."""
        from agentwatch.detectors.repeated_calls import _wait_timing
        from agentwatch.core.correlate import Call
        from agentwatch.core import schema as S

        calls = []
        for i in range(3):
            c = Call(key=f"w{i}", client="codex", session_id="s", tool_name="collaboration.wait_agent",
                     category=S.CAT_OTHER, status=S.STATUS_SUCCESS)
            c.params, c.duration_ms = {"timeout_ms": 50000}, 50000
            calls.append(c)
        wt = _wait_timing(calls, [55.0, 55.0], {"median": 105.0})
        self.assertEqual(wt["requested_s"], [50.0])
        self.assertEqual(wt["call_duration_median_s"], 50.0)
        self.assertEqual(wt["interval_median_s"], 105.0)     # l'intervalle observe est le double du delai demande
        self.assertEqual(wt["idle_median_s"], 55.0)

    def test_cross_agent_absence_is_stated(self) -> None:
        """Le rapport dit qu'aucune comparaison entre agents n'est faite."""
        view = self._session()
        cross = compute_stats(view, self.cfg)["cross_agent"]
        self.assertGreater(cross["agents"], 1)
        self.assertFalse(cross["performed"])
        self.assertIn("non effectuees", render_markdown(self._report(view)))

    # ------------------------------------------------------------------ tokens
    def test_token_kinds_are_separated(self) -> None:
        """Mesure par reponse, repartition calculee par appel, estimation (aucune) : trois natures distinctes."""
        view = self._session()
        st = compute_stats(view, self.cfg)
        kinds = st["usage"]["kinds"]
        self.assertIn("par reponse du modele", kinds["measured"])
        self.assertIn("CALCULEE", kinds["allocated"])
        self.assertIn("aucune estimation", kinds["estimated"])
        md = render_markdown(self._report(view))
        self.assertIn("Tokens, repartition calculee", md)
        self.assertIn("tokens repartis par calcul", md)
        self.assertNotIn("tokens mesures (", md)             # plus de « mesure » pour une part calculee

    def test_no_billing_wording(self) -> None:
        """Aucun rapport ne parle de ce qui a ete paye : aucune donnee de facturation n'est lue."""
        md = render_markdown(self._report(self._session()))
        for word in ("paye", "payee", "facture", "cout en euros", "$"):
            self.assertNotIn(word, md.lower())


if __name__ == "__main__":
    unittest.main()
