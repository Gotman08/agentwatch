"""Types de lignes et d'elements Codex jusque-la visibles seulement dans l'export (`inspect`) : leur import, la source
de chaque evenement, et la distinction entre importe, en attente et non interprete.

# * Rollouts SYNTHETIQUES, ecrits d'apres les formes relevees le 2026-09-19 sur 652 rollouts reels (noms de champs,
#   types et valeurs categorielles seulement ; aucun contenu reel). Les textes "SECRET" verifient que rien n'est
#   conserve en clair.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentwatch import cli
from agentwatch.collector import rollouts as R
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core.session import load_session
from agentwatch.reports.stats import session_tokens
from tests.test_rollouts import ROOT, RolloutBuilder

CHILD = "01a0aaaa-0000-7000-8000-000000000002"


def item(b: RolloutBuilder, it: dict[str, Any]) -> int:
    start = b.ms()
    b.add("event_msg", {"type": "item_completed", "thread_id": b.thread_id, "turn_id": "t", "item": it,
                        "started_at_ms": start, "completed_at_ms": start + 20})
    return len(b.lines)


class RolloutItemsTests(unittest.TestCase):
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
        self.store = EventStore(self.home, self.cfg)
        self.path = self.day / f"rollout-2026-09-19T10-00-00-{ROOT}.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------ fil synthetique
    def _build(self) -> tuple[RolloutBuilder, dict[str, int]]:
        at: dict[str, int] = {}
        b = RolloutBuilder(ROOT).meta().turn("turn-1")
        b.add("event_msg", {"type": "thread_settings_applied", "thread_id": ROOT, "thread_settings": {
            "model": "gpt-6-astra", "model_provider_id": "openai", "approval_policy": "never", "approvals_reviewer": "user",
            "permission_profile": {"SECRET": "x"}, "cwd": "C:\\proj", "reasoning_effort": "ultra", "personality": "pragmatic",
            "collaboration_mode": {"mode": "default", "settings": {}}, "reasoning_summary": "detailed", "service_tier": "priority"}})
        at["settings"] = len(b.lines)
        # * message de l'utilisateur : l'element (UI) precede la ligne response_item de meme texte -> un seul message
        item(b, {"type": "UserMessage", "id": "um-1", "content": [{"type": "text", "text": "SECRET demande de l'utilisateur",
                                                                    "text_elements": []}]})
        b.add("response_item", {"type": "message", "id": "msg_u1", "role": "user",
                                "content": [{"type": "input_text", "text": "SECRET demande de l'utilisateur"}]})
        # * raisonnement : ligne (resume + contenu chiffre) et element (resume) -> rattaches au releve de la reponse
        item(b, {"type": "Reasoning", "id": "rs-1", "summary_text": ["SECRET resume du raisonnement"], "raw_content": []})
        b.add("response_item", {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "SECRET resume du raisonnement"}],
                                "encrypted_content": "gAAAA" + "x" * 95})
        at["reasoning"] = len(b.lines)
        # * commentaire de l'agent : ligne puis element de meme texte -> un seul message
        b.add("response_item", {"type": "message", "id": "msg_a1", "role": "assistant", "phase": "commentary",
                                "content": [{"type": "output_text", "text": "SECRET je regarde le build maintenant"}]})
        item(b, {"type": "AgentMessage", "id": "am-1", "phase": "commentary",
                 "content": [{"type": "Text", "text": "SECRET je regarde le build maintenant"}]})
        # * element dont le texte est inclus dans le message deja importe : doublon aussi
        item(b, {"type": "AgentMessage", "id": "am-2", "content": [{"type": "Text", "text": "SECRET je regarde le build"}]})
        # * element sans ligne de meme texte (sous-agent reecrit, commentaire seul) : seule trace -> importe
        at["unique"] = item(b, {"type": "AgentMessage", "id": "am-3", "phase": "commentary",
                                "content": [{"type": "Text", "text": "SECRET commentaire que seul l'element porte"}]})
        # * recherche d'outils (fonction du client) : descriptions contenant "error" -> pas un echec
        b.add("response_item", {"type": "tool_search_call", "id": "ts_1", "call_id": "call_ts", "status": "completed",
                                "execution": "client", "arguments": {"query": "SECRET outils unreal", "limit": 5}})
        at["tool_search_call"] = len(b.lines)
        b.usage(3000, 2500, 40)
        at["usage1"] = len(b.lines)
        b.add("response_item", {"type": "tool_search_output", "call_id": "call_ts", "status": "completed", "execution": "client",
                                "tools": [{"type": "namespace", "name": "mcp__unreal", "description": "invalid input returns an error",
                                           "tools": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}]})
        at["tool_search_output"] = len(b.lines)
        # * outils heberges : recherche web (ouverture de page), image rendue (statut "generating")
        b.add("response_item", {"type": "web_search_call", "id": "ws_1", "status": "completed",
                                "action": {"type": "open_page", "url": "https://example.invalid/SECRET"}})
        b.add("response_item", {"type": "image_generation_call", "id": "ig_1", "status": "generating",
                                "revised_prompt": "SECRET image", "result": "iVBOR" + "A" * 200})
        # * recherche web (element) avec resultats
        at["websearch"] = item(b, {"type": "WebSearch", "id": "wsi-1", "query": "SECRET requete",
                                   "action": {"type": "search", "queries": ["SECRET requete"]}, "results": [{"title": "t", "url": "u"}]})
        # * attente d'un sous-agent : appel de fonction, puis element CollabAgentToolCall de meme identifiant
        b.fn("call_wait", "collaboration", "wait_agent", {"targets": [CHILD], "timeout_ms": 30000}, '{"status": {}}',
             items=[{"type": "CollabAgentToolCall", "id": "call_wait", "tool": "wait", "status": "completed",
                     "sender_thread_id": ROOT, "receiver_thread_ids": [CHILD], "receiver_agents": [], "agents_states": {}}])
        # * script exec qui lance un sous-agent (element seul, id propre) et envoie un message (sortie de fonction seule)
        b.exec_("call_exec", [
            {"type": "CollabAgentToolCall", "id": "collab-own", "tool": "spawn_agent", "status": "completed", "sender_thread_id": ROOT,
             "receiver_thread_ids": [CHILD], "receiver_agents": [], "agents_states": {}, "prompt": "SECRET consigne au sous-agent",
             "model": "gpt-6-astra", "reasoning_effort": "low"},
            {"type": "FunctionCallOutput", "id": "fco-1", "name": "send_message_to_thread", "namespace": "codex_app",
             "output": "SECRET message envoye"}])
        # * activite des sous-agents vue du parent ; compaction (element) ; mode vocal
        item(b, {"type": "SubAgentActivity", "id": "sa-1", "kind": "started", "agent_thread_id": CHILD, "agent_path": "/root/x"})
        item(b, {"type": "SubAgentActivity", "id": "sa-2", "kind": "interacted", "agent_thread_id": CHILD, "agent_path": "/root/x"})
        item(b, {"type": "ContextCompaction", "id": "cc-1"})
        b.add("realtime_item", {"id": "rt-1", "realtime_session_id": "rts-1", "type": "transcript_segment", "role": "user",
                                "text": "SECRET voix"})
        # * message entre agents declenchant un tour
        b.add("inter_agent_communication_metadata", {"trigger_turn": True})
        b.add("response_item", {"type": "agent_message", "id": "amsg-1", "author": "/root/x", "recipient": "/root",
                                "content": [{"type": "input_text", "text": "SECRET rapport du sous-agent"}]})
        # * releve token_count : meme mesure que token_usage_record -> doublon reconnu
        b.add("event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1}, "total_token_usage": {"input_tokens": 1}},
                            "rate_limits": {}})
        # * types inconnus : lus, comptes, jamais perdus en silence
        b.add("future_line", {"x": 1})
        b.add("event_msg", {"type": "future_event"})
        item(b, {"type": "FutureItem", "id": "f-1"})
        b.end_turn("turn-1")
        return b, at

    def _import(self, text: str) -> dict[str, Any]:
        self.path.write_text(text, encoding="utf-8", newline="\n")
        out = R.import_rollouts(self.store, self.cfg, [str(self.path)])
        self.assertEqual(out["errors"], [])
        return R.load_state(str(self.home))["files"][next(iter(R.load_state(str(self.home))["files"]))]

    # ------------------------------------------------------------------ tests
    def test_new_types_become_calls_messages_and_activity(self) -> None:
        b, at = self._build()
        st = self._import(b.text())
        v = load_session(self.store, "codex", ROOT, self.cfg)
        calls = {c.call_id: c for c in v.calls}
        # * appels : recherche d'outils (reussie malgre le mot "error" dans les descriptions), recherches web, image
        ts = calls["call_ts"]
        self.assertEqual((ts.tool_name, ts.status), ("tool_search", "success"))
        self.assertEqual((ts.evidence.get("result_count"), ts.evidence.get("result_tools")), (1, 3))
        self.assertEqual((calls["ws_1"].tool_name, calls["ws_1"].status, calls["ws_1"].evidence.get("web_action")),
                         ("web_search", "success", "open_page"))
        self.assertEqual(calls["ig_1"].status, "success")                   # * "generating" n'est pas un echec
        self.assertEqual((calls["wsi-1"].evidence.get("web_action"), (calls["wsi-1"].output_size_bytes or 0) > 0), ("search", True))
        # * attente : UN appel, complete par l'element de collaboration (destinataires, statut)
        self.assertEqual(sum(1 for c in v.calls if c.call_id == "call_wait"), 1)
        self.assertEqual((calls["call_wait"].evidence.get("collab_receivers"), calls["call_wait"].evidence.get("collab_status")),
                         ([CHILD], "completed"))
        # * appels faits depuis le script exec : lancement de sous-agent, message a un autre fil
        own = calls["collab-own"]
        self.assertEqual((own.tool_name, own.evidence.get("exec_call_id")), ("collaboration.spawn_agent", "call_exec"))
        self.assertEqual((calls["fco-1"].tool_name, calls["fco-1"].status), ("codex_app.send_message_to_thread", "success"))
        # * messages : un seul par texte ; l'element sans ligne de meme texte est importe (origine indiquee)
        msgs = [m for m in v.markers if m.phase == "message"]
        origin = [m for m in msgs if m.meta.get("origin") == "element AgentMessage"]
        self.assertEqual(len(origin), 1)
        self.assertEqual((origin[0].meta["role"], origin[0].meta.get("phase"), origin[0].meta.get("message_id")),
                         ("assistant", "commentary", "am-3"))
        self.assertEqual(sum(1 for m in msgs if m.meta.get("role") == "user"), 1)
        self.assertEqual(sum(1 for m in msgs if m.meta.get("role") == "assistant" and m.meta.get("phase") == "commentary"), 2)
        instr = [m for m in msgs if m.meta.get("role") == "agent_instruction" and m.meta.get("tool") == "collaboration.spawn_agent"]
        self.assertEqual(len(instr), 1)                                       # * consigne du sous-agent lance par script
        self.assertEqual(next(m for m in msgs if m.meta.get("role") == "agent").meta.get("trigger_turn"), True)
        # * activite : reglages, sous-agents vus du parent (pas des debuts de sous-agent), compaction, mode vocal
        act = {m.meta.get("kind"): m.meta for m in v.markers if m.phase == "activity"}
        self.assertEqual((act["settings"]["model"], act["settings"]["reasoning_effort"], act["settings"]["collaboration_mode"]),
                         ("gpt-6-astra", "ultra", "default"))
        self.assertEqual(act["subagent_activity"]["child"], CHILD)
        self.assertEqual(sum(1 for m in v.markers if m.phase == "activity" and m.meta.get("kind") == "subagent_activity"), 2)
        self.assertFalse(any(m.phase == "subagent_start" for m in v.markers))
        self.assertIn("compaction_item", act)
        self.assertEqual((act["realtime"]["type"], act["realtime"]["role"], act["realtime"]["text_chars"]),
                         ("transcript_segment", "user", len("SECRET voix")))
        # * raisonnement rattache au releve de la reponse qui suit : tailles et lignes sources, jamais le texte
        first = next(m.meta["usage"] for m in v.markers if m.phase == "usage" and m.meta["usage"].get("scope") == "response")
        rsn = first["reasoning"]
        self.assertEqual((rsn["blocks"], rsn["item_blocks"], rsn["summary_chars"], rsn["encrypted_chars"]),
                         (1, 1, len("SECRET resume du raisonnement"), 100))
        self.assertIn(at["reasoning"], rsn["lines"])
        # * etat : non interpretes comptes par type ; doublons reconnus ; rien en attente
        self.assertEqual(st["uninterpreted"], {"ligne future_line": 1, "event_msg future_event": 1, "element FutureItem": 1})
        self.assertEqual(st["duplicates"].get("element UserMessage (meme texte qu'une ligne response_item)"), 1)
        self.assertEqual(st["duplicates"].get("element AgentMessage (meme texte qu'une ligne response_item)"), 1)
        self.assertEqual(st["duplicates"].get("element AgentMessage (texte inclus dans un message deja importe)"), 1)
        self.assertEqual(st["duplicates"].get("token_count (meme mesure que token_usage_record)"), 1)
        self.assertEqual(st["pending_items"], [])

    def test_one_call_per_web_search_whatever_its_forms(self) -> None:
        # * Constate le 2026-09-19 : une recherche hebergee s'ecrit d'abord en element WebSearch puis en ligne
        #   web_search_call de meme identifiant (31 fois sur 31) ; la fonction web.run a aussi son element WebSearch
        #   de meme identifiant, avant ou apres sa sortie (210 fois sur 210).
        b = RolloutBuilder(ROOT).meta().turn("turn-1")
        item(b, {"type": "WebSearch", "id": "ws_h", "query": "SECRET q", "action": {"type": "search"}, "results": [{"t": 1}]})
        b.add("response_item", {"type": "web_search_call", "id": "ws_h", "status": "completed", "action": {"type": "search"}})
        item(b, {"type": "Extension", "id": "ig_h", "kind": "image_gen.generation", "query": None, "action": {}, "results": None})
        b.add("response_item", {"type": "image_generation_call", "id": "ig_h", "status": "generating", "result": "iVBOR"})
        b.fn("run_1", "web", "run", {"q": "SECRET"}, '[{"title": "SECRET error in results"}]',
             items=[{"type": "WebSearch", "id": "run_1", "query": "SECRET q", "action": {"type": "open_page"}, "results": [{"t": 1}, {"t": 2}]}])
        b.fn("run_2", "web", "run", {"q": "SECRET"}, '[{"title": "ok"}]')
        item(b, {"type": "WebSearch", "id": "run_2", "query": "SECRET q", "action": {"type": "search"}, "results": []})
        b.end_turn("turn-1")
        st = self._import(b.text())
        v = load_session(self.store, "codex", ROOT, self.cfg)
        self.assertEqual((v.counts["duplicate_events"], v.counts["duplicate_phases"]), (0, 0))
        per_id: dict[str, int] = {}
        for c in v.calls:
            per_id[str(c.call_id)] = per_id.get(str(c.call_id), 0) + 1
        self.assertEqual({k: per_id.get(k) for k in ("ws_h", "ig_h", "run_1", "run_2")}, {"ws_h": 1, "ig_h": 1, "run_1": 1, "run_2": 1})
        calls = {c.call_id: c for c in v.calls}
        # * web.run : statut et fin donnes par sa sortie, comme avant ; l'element ajoute l'action et le nombre de resultats
        self.assertEqual((calls["run_1"].tool_name, calls["run_1"].evidence.get("web_action"), calls["run_1"].evidence.get("web_results")),
                         ("web.run", "open_page", 2))
        self.assertEqual((calls["run_2"].evidence.get("web_action"), calls["run_2"].evidence.get("web_results")), ("search", 0))
        self.assertEqual(st["duplicates"].get("response_item web_search_call (meme appel que l'element de meme identifiant)"), 1)
        self.assertEqual(st["duplicates"].get("response_item image_generation_call (meme appel que l'element de meme identifiant)"), 1)

    def test_every_imported_event_points_to_its_source_line(self) -> None:
        b, at = self._build()
        text = b.text()
        self._import(text)
        raw = text.encode("utf-8")
        starts = [0] + [i + 1 for i, ch in enumerate(raw) if ch == 0x0A][:-1]
        events = [json.loads(line) for f in (self.home / "segments" / "codex").rglob("*.jsonl")
                  for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
        sourced = [e for e in events if (e.get("evidence") or {}).get("source")]
        thread_totals = [e for e in events if (e.get("usage") or {}).get("scope") == "thread"]
        self.assertEqual(len(sourced) + len(thread_totals), len(events))    # * seul le total du fil n'a pas de ligne unique
        for e in sourced:
            src = e["evidence"]["source"]
            self.assertEqual(src["file"], self.path.name)
            self.assertEqual(starts[src["line"] - 1], src["offset"])          # * ligne et octet designent la meme ligne
        v = load_session(self.store, "codex", ROOT, self.cfg)
        ts = next(c for c in v.calls if c.call_id == "call_ts")
        self.assertEqual((ts.evidence["source_start"]["line"], ts.evidence["source_end"]["line"]),
                         (at["tool_search_call"], at["tool_search_output"]))
        self.assertIn(at["usage1"] + 1, [s["line"] for s in [ts.evidence["source_end"]]])
        unique = next(m for m in v.markers if m.phase == "message" and m.meta.get("message_id") == "am-3")
        self.assertEqual(unique.meta["source"]["line"], at["unique"])      # * l'element importe garde SA ligne
        settings = next(m for m in v.markers if m.phase == "activity" and m.meta.get("kind") == "settings")
        self.assertEqual(settings.meta["source"]["line"], at["settings"])

    def test_nothing_secret_is_stored(self) -> None:
        b, _ = self._build()
        self._import(b.text())
        stored = "".join(f.read_text(encoding="utf-8") for f in (self.home / "segments").rglob("*.jsonl"))
        stored += (self.home / "import" / "codex-rollouts.json").read_text(encoding="utf-8")
        self.assertNotIn("SECRET", stored)
        self.assertNotIn("example.invalid", stored)

    def test_item_waits_for_its_line_across_two_reads(self) -> None:
        # * Le suivi lit par morceaux : un element ecrit juste avant la fin d'une lecture attend sa ligne de meme texte.
        b = RolloutBuilder(ROOT)
        b.t = datetime.now(timezone.utc) - timedelta(seconds=30)     # * fil actif : pas de liberation pour inactivite
        b.meta().turn("turn-1")
        item(b, {"type": "AgentMessage", "id": "am-a", "content": [{"type": "Text", "text": "SECRET double a venir"}]})
        item(b, {"type": "AgentMessage", "id": "am-b", "content": [{"type": "Text", "text": "SECRET sans double"}]})
        first = b.text()
        st = self._import(first)
        self.assertEqual(len(st["pending_items"]), 2)                     # * en attente, pas encore des messages
        b.add("response_item", {"type": "message", "id": "msg_x", "role": "assistant",
                                "content": [{"type": "output_text", "text": "SECRET double a venir"}]})
        for i in range(12):
            b.add("event_msg", {"type": "task_started", "turn_id": f"t{i}", "model_context_window": 1})
        st = self._import(b.text())
        self.assertEqual(st["pending_items"], [])
        v = load_session(self.store, "codex", ROOT, self.cfg)
        ids = sorted(str(m.meta.get("message_id")) for m in v.markers if m.phase == "message")
        self.assertEqual(ids, ["am-b", "msg_x"])                          # * am-a : meme texte que msg_x ; am-b : seule trace
        self.assertEqual(st["line_no"], len(b.lines))

    def test_idle_file_releases_items_without_line(self) -> None:
        # * Fil inactif depuis 10 min (lu jusqu'au bout) : l'element en attente n'aura pas de double, il est importe.
        b = RolloutBuilder(ROOT)
        b.t = datetime.now(timezone.utc) - timedelta(hours=1)
        b.meta().turn("turn-1")
        item(b, {"type": "AgentMessage", "id": "am-z", "content": [{"type": "Text", "text": "SECRET derniere trace"}]})
        st = self._import(b.text())
        self.assertEqual(st["pending_items"], [])
        v = load_session(self.store, "codex", ROOT, self.cfg)
        self.assertEqual([m.meta.get("message_id") for m in v.markers if m.phase == "message"], ["am-z"])

    def test_old_format_token_count_is_the_only_measure(self) -> None:
        # * Rollouts de juin a aout : pas de token_usage_record, seulement des releves token_count (dernier et cumul).
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")

        def tc(last: tuple[int, int, int], total: tuple[int, int, int]) -> None:
            def u(t: tuple[int, int, int]) -> dict[str, int]:
                return {"input_tokens": t[0], "cached_input_tokens": t[1], "output_tokens": t[2], "total_tokens": t[0] + t[2]}
            b.add("event_msg", {"type": "token_count", "info": {"last_token_usage": u(last), "total_token_usage": u(total),
                                                                "model_context_window": 400000}, "rate_limits": {}})

        tc((1000, 800, 50), (1000, 800, 50))
        tc((1000, 800, 50), (1000, 800, 50))           # * limites de debit seulement : cumul inchange, pas une reponse
        tc((2000, 1900, 70), (3000, 2700, 120))
        b.add("event_msg", {"type": "token_count", "info": None, "rate_limits": {}})
        st = self._import(b.text())
        v = load_session(self.store, "codex", ROOT, self.cfg)
        per = [m.meta["usage"] for m in v.markers if m.phase == "usage" and m.meta["usage"].get("scope") == "response"]
        self.assertEqual([(u["input_tokens"], u["format"]) for u in per], [(1000, "token_count"), (2000, "token_count")])
        tot = session_tokens(v)
        self.assertEqual((tot["requests"], tot["input_tokens"], tot["cached_input_tokens"], tot["output_tokens"]), (2, 3000, 2700, 120))
        thread = next(m.meta["usage"] for m in v.markers if m.phase == "usage" and m.meta["usage"].get("scope") == "thread")
        self.assertEqual(thread["format"], "token_count")
        self.assertEqual(st["duplicates"].get("token_count repete (cumul inchange)"), 1)
        self.assertEqual(st["duplicates"].get("token_count sans usage (quota seulement)"), 1)
        # * l'export compte de meme : un releve repete n'est ni une reponse, ni des tokens de plus
        import io
        from agentwatch.reports import inspect as INS
        buf = io.StringIO()
        INS.export_session(self.cfg, str(self.home), ROOT, buf)
        out = buf.getvalue()
        self.assertIn("; 2 reponse(s) du modele ;", out)
        self.assertIn("Tokens MESURES par Codex (token_count (ancien format), une fois par reponse) : entree 3000 dont cache 2700", out)
        self.assertIn("Releve token_count repete (cumul du fil inchange", out)

    def test_state_from_before_line_numbers_is_counted_once(self) -> None:
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        st = self._import(b.text())
        n = len(b.lines)
        norm = next(iter(R.load_state(str(self.home))["files"]))
        state = R.load_state(str(self.home))
        state["files"][norm].pop("line_no")                               # * etat ecrit par une version anterieure
        R.save_state(str(self.home), state)
        b.add("event_msg", {"type": "task_complete", "turn_id": "turn-1", "duration_ms": 10})
        self.path.write_text(b.text(), encoding="utf-8", newline="\n")
        R.import_rollouts(self.store, self.cfg, [str(self.path)])
        v = load_session(self.store, "codex", ROOT, self.cfg)
        end = next(m for m in v.markers if m.phase == "turn_end")
        self.assertEqual(end.meta["source"]["line"], n + 1)
        self.assertEqual(st["line_no"], n)

    def test_doctor_lines_separate_imported_pending_and_uninterpreted(self) -> None:
        b, _ = self._build()
        self._import(b.text())
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"timestamp": "2026-09-19T11:00:00.000Z", "type": "event_msg",
                                 "payload": {"type": "task_started", "turn_id": "t2"}}) + "\n")
        lines = cli._rollout_state_lines(self.cfg, R.load_state(str(self.home)))
        text = "\n".join(lines)
        self.assertIn("importees : 1 rollout(s) suivi(s)", text)
        self.assertRegex(text, r"en attente de lecture : 1 rollout\(s\), \d+ octet\(s\) pas encore lus, retard \d+ s")
        self.assertIn("lues mais non interpretees (sur 1 rollout(s) lus par cette version) : ", text)
        self.assertIn("ligne future_line x1", text)
        self.assertIn("reconnues sans nouvel evenement", text)


class ReportTrailerTests(unittest.TestCase):
    """Constate le 2026-09-21 (appels #6101 et #6140 de la session 01a0bf95) : pour un lanceur en echec, le resume d'erreur
    (fin de la sortie) n'etait plus que la ligne `{"resumes_ecrits": ...}` ajoutee apres coup ; la cause etait juste au-dessus."""

    TRAILER = ('{"resumes_ecrits":[{"resultat":"Saved/Wave/Build20/inputs.json","octets":93538,"resume":"Saved/Wave/Build20/'
               'inputs.summary.json","caracteres":5899}],"lire":"le resume d\'abord ; un detail : python Scripts/Agent/agent_peek.py '
               '<resultat> --pointer <pointeur> ; le resultat complet est inchange"}')

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, sessions = root / "home", root / "sessions"
        self.day = sessions / "2026" / "09" / "21"
        self.day.mkdir(parents=True)
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"health": {"codex_sessions_dir": str(sessions)},
                                                           "rollouts": {"background_priority": False}}), encoding="utf-8")
        self.cfg = load_config(self.home)
        self.store = EventStore(self.home, self.cfg)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _calls(self, outputs: dict[str, str]) -> dict[str, Any]:
        b = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        for i, (item_id, out) in enumerate(outputs.items()):
            b.exec_(f"call_{i}", [RolloutBuilder.cmd(item_id, "& './Scripts/Delivery/build_extended_checkpoint.ps1' -Checkpoint Build20", out, code=1)])
        b.usage(1200, 1100, 30)
        path = self.day / f"rollout-2026-09-21T10-00-00-{ROOT}.jsonl"
        path.write_text(b.text(), encoding="utf-8", newline="\n")
        self.assertEqual(R.import_rollouts(self.store, self.cfg, [str(path)])["errors"], [])
        view = load_session(self.store, "codex", ROOT, self.cfg)
        return {c.call_id: c for c in view.calls}

    def test_report_trailer_never_replaces_the_failure_diagnosis(self) -> None:
        from agentwatch.core.normalize import classify_error
        build = 'Build failed.\n{\n  "ExitCode": 6,\n  "Target": "ItemsEditor Win64 Development"\n}\n'
        calls = self._calls({"exec-a": build + self.TRAILER + "\n", "exec-b": self.TRAILER + "\n",
                             "exec-c": build + "Etat courant regenere : Docs/PlayerLoop/ETAT_COURANT.md (13647 caracteres)\n" + self.TRAILER,
                             "exec-d": 'Traceback\n{"ExitCode": 3, "resume": "le script rend son propre JSON en derniere ligne"}\n'})
        a, b, c, d = (calls[k] for k in ("exec-a", "exec-b", "exec-c", "exec-d"))
        self.assertEqual({x.status for x in (a, b, c, d)}, {"error"})                      # * un echec reste un echec
        self.assertIn('"ExitCode": 6', a.error_summary)                                    # * avant : la ligne resumes_ecrits
        self.assertNotIn("resumes_ecrits", a.error_summary)
        self.assertEqual(classify_error(a.error_summary)["detail"], "ExitCode 6 ecrit par le script")
        self.assertEqual(a.evidence.get("error_trailer_lines_removed"), 1)
        self.assertEqual((b.error_summary, b.error_signature), (None, None))               # * rien d'autre dans la sortie : cause INCONNUE
        self.assertIn("unknown", b.evidence.get("error_cause", ""))
        self.assertEqual((c.evidence.get("error_trailer_lines_removed"), classify_error(c.error_summary)["script_exit_code"]), (2, 6))
        self.assertIn('"ExitCode": 3', d.error_summary)                                    # * le JSON du script lui-meme n'est pas retire

    def test_removing_the_trailer_touches_the_diagnosis_only(self) -> None:
        """Le retrait ne sert qu'a extraire le diagnostic : tailles, empreintes, poids de partage et sources restent ceux de
        la sortie ENTIERE, ligne de compte rendu comprise."""
        build = 'Build failed.\n{\n  "ExitCode": 6\n}\n'
        with_trailer = self._calls({"exec-a": build + self.TRAILER + "\n"})["exec-a"]
        full = build + self.TRAILER + "\n"
        self.assertGreaterEqual(with_trailer.output_size_bytes, len(full))                 # * la ligne ajoutee compte dans la taille brute
        self.assertEqual(with_trailer.evidence["delivered_chars"], len(full))              # * et dans ce qui est livre au modele
        self.assertTrue(with_trailer.evidence["source_end"]["line"])                       # * preuve source conservee
        self.tmp.cleanup()
        self.setUp()
        without = self._calls({"exec-a": build})["exec-a"]
        self.assertNotEqual(with_trailer.result_fingerprint, without.result_fingerprint)   # * l'empreinte porte sur toute la sortie
        self.assertLess(without.output_size_bytes, with_trailer.output_size_bytes)
        self.assertEqual(with_trailer.error_summary.strip(), without.error_summary.strip())  # * seul le diagnostic est le meme

    def test_stored_summary_that_is_only_a_trailer_means_unknown_cause(self) -> None:
        """Import anterieur : le magasin ne garde que la fin de la ligne de compte rendu. La cause reste inconnue."""
        from agentwatch.core import normalize as N
        stored = self.TRAILER[-400:]
        self.assertTrue(N.is_report_trailer_fragment(stored))
        self.assertFalse(N.is_report_trailer_fragment('  "ExitCode": 6,\n}'))
        calls = self._calls({"exec-a": 'Build failed.\n{"ExitCode": 6}\n'})
        import agentwatch.core.correlate as CO
        events, _ = self.store.read_session_events("codex", ROOT)
        for ev in events:
            if ev.get("call_id") == "exec-a" and ev.get("error_summary"):
                ev["error_summary"], ev["error_signature"] = stored, N.make_error_signature(stored)
        call = next(c for c in CO.build_session(events, self.cfg).calls if c.call_id == "exec-a")
        self.assertEqual((call.status, call.error_summary, call.error_signature), ("error", None, None))
        self.assertIn("reimport", call.evidence["error_cause"])
        self.assertTrue(calls)
        # * la fin stockee tenait sur plusieurs lignes : la ligne de compte rendu est retiree, le reste est dit PARTIEL
        mixed = "\r\n".join(['L56702186",', '  "InputsChangedDuringBuild": []', "}", self.TRAILER[:200]])
        for ev in events:
            if ev.get("call_id") == "exec-a" and ev.get("error_summary"):
                ev["error_summary"] = mixed
        call = next(c for c in CO.build_session(events, self.cfg).calls if c.call_id == "exec-a")
        self.assertNotIn("resumes_ecrits", call.error_summary)
        self.assertIn("InputsChangedDuringBuild", call.error_summary)
        self.assertTrue(call.evidence["error_cause"].startswith("partial"))


if __name__ == "__main__":
    unittest.main()
