"""inspect : le detail d'une session Codex (consignes, messages, appels, resultats, tokens), relu dans les rollouts.

Exigences (2026-09-19) : retrouver l'agent principal, les sous-agents et leurs liens, les consignes et messages,
chaque appel avec ses arguments, son script, son resultat, son erreur et sa duree fiable ; rattacher les tokens aux
reponses ; garder la source de chaque evenement ; signaler l'absent, le tronque, le masque, le non compris ; garder
consultables les fils aux horodatages non fiables ; separer tokens mesures et parts calculees ; separer les
conclusions des detecteurs, qui ne filtrent rien.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentwatch import cli
from agentwatch.collector import rollouts as R
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.reports import inspect as INS
from tests.test_rollouts import CHILD, ROOT, RolloutBuilder

SECRET = "hunter2hunter2hunter2"


class InspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, sessions = root / "home", root / "sessions"
        self.day = sessions / "2026" / "09" / "19"
        self.day.mkdir(parents=True)
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"health": {"codex_sessions_dir": str(sessions)},
                                                           "rollouts": {"background_priority": False}}), encoding="utf-8")
        self.cfg = load_config(self.home)
        b = RolloutBuilder(ROOT).meta().turn("turn-1", f"Construis le jeu. Mot de passe du serveur : password={SECRET}")
        b.add("event_msg", {"type": "thread_settings_applied", "thread_settings": {"model": "gpt-6-astra", "reasoning_effort": "high"}})
        b.add("response_item", {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Je planifie le build."}],
                                "encrypted_content": "gAAAAB" + "x" * 300})
        b.exec_("call_1", [b.cmd("exec-a", "Get-Content Source/a.cpp", "ligne\n" * 3000),
                           b.mcp("exec-b", "romeo", "job_status", {"job_id": 42}, "Romeo n'est pas actif", is_error=True)])
        b.fn("call_2", "collaboration", "spawn_agent", {"agent_type": "worker", "message": "Verifie les tests du module Inventory."},
             '{"agent_id": "' + CHILD + '"}')
        b.add("response_item", {"type": "function_call", "id": "fc_open", "name": "wait_agent", "namespace": "collaboration",
                                "arguments": '{"targets": ["w"]}', "call_id": "call_open"})
        b.add("mystery_line", {"foo": 1, "bar": "baz"})
        b.end_turn("turn-1")
        (self.day / f"rollout-2026-09-19T10-00-00-{ROOT}.jsonl").write_text(b.text(), encoding="utf-8", newline="\n")
        # * Sous-agent reecrit d'un bloc : toutes ses lignes au meme instant, contenu intact.
        c = RolloutBuilder(CHILD, parent=ROOT, nickname="Sagan").meta().turn("turn-c", "Tache recue")
        for i in range(8):
            c.exec_(f"cc{i}", [c.cmd(f"exec-c{i}", f"python -m pytest tests/test_{i}.py", f"{i} passed")])
        c.end_turn("turn-c")
        frozen = "\n".join(re.sub(r'"timestamp": "[^"]+"', '"timestamp": "2026-07-01T10:00:00.000Z"', line) for line in c.lines) + "\n"
        (self.day / f"rollout-2026-09-19T10-05-00-{CHILD}.jsonl").write_text(frozen, encoding="utf-8", newline="\n")
        store = EventStore(self.home, self.cfg)
        R.import_rollouts(store, self.cfg, R.list_rollouts(self.cfg, None))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _export(self, **kw: object) -> tuple[str, dict]:
        buf = io.StringIO()
        summary = INS.export_session(self.cfg, str(self.home), ROOT[:13], buf, max_chars=2000, **kw)  # type: ignore[arg-type]
        return buf.getvalue(), summary

    def test_tree_messages_calls_results_and_sources(self) -> None:
        out, summary = self._export()
        self.assertEqual([t["thread_id"] for t in summary["threads"]], [ROOT, CHILD])     # * principal d'abord
        self.assertIn(f"| `{CHILD}` | Sagan (worker) | `{ROOT}` |", out)                    # * lien parent-enfant
        self.assertIn("Verifie les tests du module Inventory.", out)                       # * consigne donnee au sous-agent
        self.assertIn("Get-Content Source/a.cpp", out)                                     # * script de la commande
        self.assertIn("Romeo n'est pas actif", out)                                        # * resultat (erreur) de l'outil MCP
        self.assertIn("isError=True", out)
        self.assertIn("1250 ms (mesuree par Codex)", out)                                  # * duree fiable, et sa source
        self.assertIn("modele `gpt-6-astra`", out)                                         # * modele du fil (Astra)
        self.assertRegex(out, r"- `L\d+` \d\d:\d\d:\d\d\.\d{3} \*\*Commande `exec-a`")        # * source : ligne et heure
        self.assertIn(f"rollout-2026-09-19T10-00-00-{ROOT}.jsonl", out)

    def test_secrets_are_masked_and_everything_missing_is_flagged(self) -> None:
        out, summary = self._export()
        self.assertNotIn(SECRET, out)
        self.assertIn("<secret:", out)
        self.assertIn("[masque] 1 secret(s)", out)
        self.assertIn("[tronque] 2000 car. affiches sur", out)                             # * sortie de 18 000 car.
        self.assertIn("[absent] pas de sortie pour l'appel `call_open`", out)             # * appel reste ouvert
        self.assertIn("[non compris]", out)                                                # * type de ligne inconnu, montre
        self.assertIn("mystery_line", out)
        self.assertIn("[chiffre] 306 car. illisibles", out)                                # * raisonnement chiffre
        self.assertIn("[omis] instructions de base de Codex", out)
        for f in ("[masque]", "[tronque]", "[absent]", "[non compris]", "[chiffre]", "[omis]"):
            self.assertGreaterEqual(summary["flags"].get(f, 0), 1, f)

    def test_measured_tokens_per_response_and_computed_shares_per_call(self) -> None:
        out, _ = self._export()
        self.assertIn("tokens MESURES par Codex : entree", out)
        self.assertIn("consomme les sorties de :", out)
        self.assertIn("repartition CALCULEE par AgentWatch, pas une mesure", out)
        self.assertIn("Tokens MESURES par Codex (token_usage_record, une fois par reponse)", out)

    def test_thread_rewritten_in_one_block_stays_readable(self) -> None:
        out, summary = self._export()
        child = next(t for t in summary["threads"] if t["thread_id"] == CHILD)
        self.assertTrue(child["bulk"])
        section = out.split(f"`{CHILD}`", 2)[-1]
        self.assertIn("[horodatage non fiable]", section)
        self.assertIn("python -m pytest tests/test_7.py", section)                          # * contenu intact
        self.assertIn("7 passed", section)

    def test_conclusions_are_separate_and_filter_nothing(self) -> None:
        with_c, s1 = self._export()
        without, s2 = self._export(conclusions=False)
        self.assertEqual(s1["kinds"], s2["kinds"])                                         # * memes evenements
        self.assertIn("## Annexe : conclusions des detecteurs", with_c)
        self.assertNotIn("## Annexe", without)
        self.assertLess(with_c.index("## Recapitulatif des signalements"), with_c.index("## Annexe"))

    def test_jsonl_keeps_the_source_of_each_event(self) -> None:
        buf = io.StringIO()
        INS.export_session(self.cfg, str(self.home), ROOT, buf, fmt="jsonl")
        rows = [json.loads(line) for line in buf.getvalue().splitlines()]
        self.assertTrue(rows)
        self.assertTrue(all({"file", "line", "ordinal", "ts"} <= set(r["source"]) for r in rows))
        self.assertNotIn(SECRET, buf.getvalue())

    def test_cli_writes_a_local_file(self) -> None:
        out_path = Path(self.tmp.name) / "export.md"
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), "inspect", "--session", ROOT[:13], "--out", str(out_path)]), 0)
        self.assertTrue(out_path.is_file())
        self.assertIn("export ecrit", buf.getvalue())
        self.assertNotIn(SECRET, out_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
