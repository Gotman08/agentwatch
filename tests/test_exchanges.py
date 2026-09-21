"""Entre agents : un message envoye est rapproche de sa reception par son contenu transmis (jamais par l'ordre), une
requete qui n'emet qu'un message est comptee avec tout le contexte qu'elle relit, et une ressource touchee par
plusieurs agents est decrite (reference commune, passation, ecriture partagee).

# * Rollouts SYNTHETIQUES, ecrits d'apres les formes relevees le 2026-09-21 sur la session Codex 01a0bf95 (noms de
#   champs et en-tetes seulement ; aucun contenu reel). Constats d'origine : le texte des messages est chiffre et le
#   MEME jeton est ecrit a l'envoi et a la reception (1 139 sur 1 139) ; l'ordre par file emetteur -> destinataire se
#   trompe 107 fois sur 1 139 ; 1 140 requetes sur 4 548 n'emettent qu'un message (25 % de l'entree) ; la premiere
#   requete d'une fenetre d'un sous-agent grossit avec le cumul des messages recus (r = 0,997).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agentwatch.collector import rollouts as R
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core.session import load_session
from agentwatch.reports.exchanges import agent_exchanges, pair_messages
from tests.test_rollouts import CHILD, ROOT, RolloutBuilder

CHILD_PATH = "/root/tache"


def token(letter: str, size: int) -> str:
    """Jeton chiffre synthetique, de la forme ecrite par le client."""
    return "gAAAAAB" + letter * (size - 7)


def send(b: RolloutBuilder, call_id: str, target: str, payload: str, tool: str = "send_message") -> None:
    key = "message" if tool == "send_message" else "task"
    b.fn(call_id, "collaboration", tool, {"target": target, key: payload}, "")


def receive(b: RolloutBuilder, author: str, recipient: str, payload: str, kind: str = "MESSAGE", ms: int = 100,
            encrypted: bool = True) -> None:
    b.add("inter_agent_communication_metadata", {"trigger_turn": kind == "NEW_TASK"}, ms=ms)
    head = f"Message Type: {kind}\nTask name: {recipient}\nSender: {author}\nPayload:\n"
    content: list[dict[str, Any]] = [{"type": "input_text", "text": head if encrypted else head + payload}]
    if encrypted:
        content.append({"type": "encrypted_content", "encrypted_content": payload})
    b.add("response_item", {"type": "agent_message", "id": f"amsg_{b.ordinal}", "author": author, "recipient": recipient,
                            "content": content})


class ExchangesTests(unittest.TestCase):
    def setUp(self) -> None:
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

    def _view(self, *builders: RolloutBuilder) -> Any:
        paths = []
        for b in builders:
            p = self.day / f"rollout-2026-09-21T10-00-00-{b.thread_id}.jsonl"
            p.write_text(b.text(), encoding="utf-8", newline="\n")
            paths.append(str(p))
        out = R.import_rollouts(self.store, self.cfg, paths)
        self.assertEqual(out["errors"], [])
        return load_session(self.store, "codex", ROOT, self.cfg)

    # ------------------------------------------------------------------ rapprochement envoi -> reception
    def _out_of_order(self) -> tuple[RolloutBuilder, RolloutBuilder]:
        main = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        send(main, "call_m1", "tache", token("a", 127))
        send(main, "call_m2", "tache", token("b", 150))
        send(main, "call_m3", "tache", token("c", 140))                       # * jamais entre chez le destinataire
        child = RolloutBuilder(CHILD, parent=ROOT, nickname="Sagan").meta().turn("turn-c")
        receive(child, "/root", CHILD_PATH, token("b", 150), ms=5_000)         # * livre AVANT le premier envoye
        receive(child, "/root", CHILD_PATH, token("a", 127), ms=3_000)
        child.end_turn("turn-c")
        receive(main, CHILD_PATH, "/root", "SECRET REPONSE FINALE du sous-agent.", kind="FINAL_ANSWER", ms=12_000, encrypted=False)
        return main, child

    def test_messages_are_paired_by_transmitted_content_not_by_order(self) -> None:
        v = self._view(*self._out_of_order())
        pm = pair_messages(v)
        self.assertEqual({p["call_id"]: p["payload_chars"] for p in pm["pairs"]}, {"call_m1": 127, "call_m2": 150})
        first, second = (next(p for p in pm["pairs"] if p["call_id"] == c) for c in ("call_m1", "call_m2"))
        self.assertGreater(first["delay_s"], second["delay_s"])               # * le premier envoye est entre en dernier
        self.assertEqual((first["from"], first["to"], first["kind"], first["encrypted"]), ("main", CHILD, "MESSAGE", True))
        self.assertTrue(first["sent_source"]["line"] and first["received_source"]["line"])   # * preuve : lignes des deux rollouts
        m = agent_exchanges(v, self.cfg)["messages"]
        self.assertEqual((m["sent"], m["received"], m["pairing"]["paired"]), (3, 3, 2))
        self.assertEqual(m["pairing"]["undelivered_total"], 1)
        self.assertEqual(m["pairing"]["undelivered"][0]["to"], CHILD)
        self.assertEqual(m["pairing"]["received_without_send"], 1)             # * reponse finale d'un tour : aucun envoi en face
        self.assertEqual(m["received_by_kind"], {"MESSAGE": 2, "FINAL_ANSWER": 1})
        self.assertEqual((m["encrypted_sent"], m["encrypted_received"]), (3, 2))
        route = next(r for r in m["routes"] if (r["from"], r["to"]) == ("main", CHILD))
        self.assertEqual((route["sent"], route["received"], route["paired"], route["payload_chars"]), (3, 2, 2, 277))

    def test_a_refused_send_is_not_reported_as_a_lost_message(self) -> None:
        """Constate le 2026-09-21 (session 01a0bb58) : « 1 envoi jamais entre chez le destinataire » pour un appel que le
        client avait refuse (« collab tool failed: agent thread limit reached ») : rien n'avait ete envoye."""
        main = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        main.fn("call_t1", "collaboration", "followup_task", {"target": "tache", "message": token("a", 127)},
                "collab tool failed: agent thread limit reached")
        send(main, "call_m2", "tache", token("b", 150))                       # * envoye et entre
        send(main, "call_m3", "tache", token("c", 140))                       # * envoye, jamais entre (fin de session)
        main.usage(900, 800, 10)
        child = RolloutBuilder(CHILD, parent=ROOT, nickname="Sagan").meta().turn("turn-c")
        receive(child, "/root", CHILD_PATH, token("b", 150), ms=5_000)
        child.end_turn("turn-c")
        v = self._view(main, child)
        pg = agent_exchanges(v, self.cfg)["messages"]["pairing"]
        self.assertEqual((pg["undelivered_total"], pg["undelivered_failed_calls"]), (2, 1))       # * avant : 2 « jamais entres », sans distinction
        by_tool = {u["tool"]: u for u in pg["undelivered"]}
        self.assertEqual(by_tool["collaboration.followup_task"]["call_status"], "error")
        self.assertIn("agent thread limit reached", by_tool["collaboration.followup_task"]["call_error"])
        self.assertIsNone(by_tool["collaboration.send_message"]["call_error"])
        from agentwatch.reports.markdown import _render_exchanges
        text = "\n".join(_render_exchanges(agent_exchanges(v, self.cfg), {"agent_labels": {"main": "principal", CHILD: "Sagan"}}))
        self.assertIn("dont 1 appel(s) d'envoi en echec : rien n'a ete envoye", text)
        self.assertIn("appel d'envoi en echec : `collab tool failed: agent thread limit reached`", text)

    def test_encrypted_content_has_no_similarity_signature_and_is_never_stored(self) -> None:
        v = self._view(*self._out_of_order())
        sent = [m for m in v.markers if m.phase == "message" and m.meta.get("role") == "agent_instruction"]
        self.assertTrue(all(m.meta.get("encrypted") and "paragraph_sigs" not in m.meta for m in sent))
        stored = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in self.home.rglob("*") if p.is_file())
        self.assertNotIn("gAAAAAB", stored)
        self.assertNotIn("REPONSE FINALE", stored)

    def test_nothing_is_paired_without_the_fingerprint_on_both_sides(self) -> None:
        """Import anterieur : la reception n'a pas d'empreinte. L'ordre ne rapproche pas sans erreur : rien n'est devine."""
        v = self._view(*self._out_of_order())
        for m in v.markers:
            if m.phase == "message" and m.meta.get("role") == "agent":
                m.meta.pop("payload_fp", None)
        ex = agent_exchanges(v, self.cfg)
        self.assertEqual((ex["messages"]["pairing"]["available"], ex["messages"]["pairing"]["paired"]), (False, 0))
        self.assertIn("reimporter", ex["limits"][0])

    # ------------------------------------------------------------------ requetes qui ne font qu'echanger
    def test_message_only_requests_carry_their_whole_context_and_chain(self) -> None:
        main = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        main.exec_("call_w", [main.cmd("exec-w", "rg foo Source", "match")], usage=(30_000, 29_000, 50))
        send(main, "call_m1", "tache", token("a", 127))                       # * sa reponse : 800 tokens d'entree
        child = RolloutBuilder(CHILD, parent=ROOT, nickname="Sagan").meta().turn("turn-c")
        receive(child, "/root", CHILD_PATH, token("a", 127), ms=5_000)
        send(child, "call_c1", "/root", token("z", 133))                      # * premiere reponse apres l'entree : un message
        child.exec_("call_cw", [child.cmd("exec-cw", "rg bar Source", "match")], usage=(9_000, 8_000, 40))
        receive(main, CHILD_PATH, "/root", token("z", 133), ms=9_000)
        main.exec_("call_w2", [main.cmd("exec-w2", "rg baz Source", "match")], usage=(31_000, 30_000, 50))
        main.usage(31_200, 31_000, 40)                                        # * la reponse qui consomme la sortie
        rq = agent_exchanges(self._view(main, child), self.cfg)["message_requests"]
        self.assertEqual((rq["message_only"], rq["message_only_input_tokens"]), (2, 1_600))
        self.assertEqual(rq["by_agent"]["main"]["message_only"], 1)
        self.assertEqual(rq["share_of_session_input"], round(1_600 / (30_000 + 800 + 31_000 + 31_200 + 800 + 9_000), 4))
        # * libelles observables : ce que la premiere reponse EMET, pas ce qu'elle « veut dire »
        self.assertEqual(rq["reactions"], {CHILD: {"message_to_sender": 1}, "main": {"tool_call": 1}})
        ch = rq["chains"]
        self.assertEqual((ch["chains"], ch["messages"], ch["intermediate_input_tokens"], ch["by_length"]), (1, 2, 800, {2: 1}))
        self.assertEqual(ch["shapes"][0]["agents"], ["main", CHILD, "main"])

    # ------------------------------------------------------------------ messages gardes a travers les compactions
    def test_first_request_of_each_window_is_set_against_received_messages(self) -> None:
        main = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        child = RolloutBuilder(CHILD, parent=ROOT, nickname="Sagan").meta().turn("turn-c")
        got = 0
        for window, n in enumerate((1, 2, 1), start=1):
            for i in range(n):
                got += 1
                send(main, f"call_m{got}", "tache", token(chr(96 + got), 1_000))
                receive(child, "/root", CHILD_PATH, token(chr(96 + got), 1_000), ms=2_000)
            child.usage(90_000, 80_000, 500)                                  # * demande de compaction
            child.add("compacted", {"message": "SECRET SUMMARY", "window_number": window, "replacement_history": []})
            child.usage(40_000 + 190 * got, 30_000, 100)                      # * 0,19 token par caractere transmis
        rows = {r["agent"]: r for r in agent_exchanges(self._view(main, child), self.cfg)["resident_messages"]}
        row = rows[CHILD]
        self.assertEqual((row["windows_after_compaction"], row["received"], row["received_payload_chars"]), (3, 4, 4_000))
        self.assertEqual((row["first"]["input_tokens"], row["last"]["input_tokens"], row["last"]["messages"]), (40_190, 40_760, 4))
        self.assertEqual((row["tokens_per_payload_char"], row["correlation"]), (0.19, 1.0))
        self.assertNotIn("main", rows)                                        # * le fil principal n'a rien recu
        # * surplus par rapport a la premiere fenetre apres compaction (40 190) x requetes de la fenetre :
        #   fenetre 2 : 380 x 2 requetes (premiere requete + demande de compaction), fenetre 3 : 570 x 1
        self.assertEqual((row["window_start_surplus_tokens"], row["requests_in_these_windows"]), (380 * 2 + 570, 5))

    def test_retained_context_estimate_is_separate_and_never_a_gain(self) -> None:
        main = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        child = RolloutBuilder(CHILD, parent=ROOT, nickname="Sagan").meta().turn("turn-c")
        for window in (1, 2, 3):
            send(main, f"call_m{window}", "tache", token(chr(96 + window), 1_000))
            receive(child, "/root", CHILD_PATH, token(chr(96 + window), 1_000), ms=2_000)
            child.usage(90_000, 80_000, 500)
            # * la compaction d'un sous-agent garde tels quels les messages d'agents recus (forme relevee le 2026-09-21)
            child.add("compacted", {"message": "SECRET SUMMARY", "window_number": window,
                                    "replacement_history": [{"type": "message", "role": "developer"}, {"type": "compaction"}]
                                    + [{"type": "agent_message", "content": [{"type": "encrypted_content", "encrypted_content": "SECRET"}]}] * window})
            child.usage(40_000 + 190 * window, 30_000, 100)
        main.usage(900, 800, 10)                                              # * la reponse qui consomme le dernier envoi
        v = self._view(main, child)
        ex = agent_exchanges(v, self.cfg)
        est = ex["retained_context_estimate"]
        self.assertEqual((est["nature"], est["agents"], est["tokens"]), ("estimation", [CHILD], 190 * 2 + 380))
        self.assertEqual(est["agent_basis"], {CHILD: "fait releve a la compaction"})
        row = next(r for r in ex["resident_messages"] if r["agent"] == CHILD)
        self.assertEqual(row["compactions"], [{"kept_agent_messages": n, "received_before": n} for n in (1, 2, 3)])
        self.assertEqual((row["compactions_observed"], row["compactions_with_fact"]), (3, 3))
        # * le RELEVE (des comptes de messages) reste a part de l'ESTIMATION (des tokens)
        self.assertEqual(est["kept_messages_fact"], {"agents_with_fact": 1, "agents_without_fact": 0, "kept_at_last_compaction": 3,
                                                     "received_before_last_compaction": 3})
        self.assertEqual(est["not_additive_with"], "message_requests.message_only_input_tokens")
        # * compare : le releve et l'estimation sont dans deux natures differentes, et aucun scenario n'en est derive
        from agentwatch.reports import compare as C
        ctx = C.measure(v, self.cfg)["context"]
        self.assertEqual((ctx["exact"]["message_only_requests"], ctx["exact"]["message_only_input_tokens"]), (3, 2_400))
        self.assertEqual(ctx["attributed"]["retained_context_estimate_tokens"], 190 * 2 + 380)
        self.assertNotIn("retained_context_estimate_tokens", ctx["exact"])
        self.assertEqual((ctx["exact"]["kept_fact_known_sessions"], ctx["exact"]["kept_agent_messages_at_last_compaction"],
                          ctx["exact"]["received_before_last_compaction"]), (1, 3, 3))
        self.assertEqual(ctx["attributed"]["retained_context_agents_by_basis"], {"fait releve a la compaction": 1})
        m = C.merge([C.measure(v, self.cfg)])
        rows = {r["key"]: r["before"] for nature in ("exact", "attributed") for r in C._context_rows(m, m, nature)}
        self.assertEqual(rows["message_only_input_tokens"], 2_400)
        self.assertEqual(rows["retained_context_estimate"], 190 * 2 + 380)
        self.assertEqual((rows["kept_agent_messages_share"], rows["retained_basis_fact_agents"], rows["retained_basis_correlation_agents"]),
                         (100.0, 1, 0))
        # * une mesure enregistree AVANT ces champs (la reference du 2026-09-21) : « - », jamais zero
        old = json.loads(json.dumps(m))
        for k in ("kept_fact_known_sessions", "kept_fact_agents", "kept_fact_agents_missing", "kept_agent_messages_at_last_compaction",
                  "received_before_last_compaction"):
            old["context"]["exact"].pop(k)
        old["context"]["attributed"].pop("retained_context_agents_by_basis")
        old_rows = {r["key"]: r["before"] for nature in ("exact", "attributed") for r in C._context_rows(old, old, nature)}
        self.assertEqual((old_rows["kept_agent_messages_share"], old_rows["retained_basis_fact_agents"],
                          old_rows["retained_basis_correlation_agents"]), (None, None, None))
        self.assertEqual(old_rows["retained_context_estimate"], 190 * 2 + 380)                # * l'estimation, elle, reste lisible
        self.assertFalse([s for s in C._scenarios(m) if "message" in s["key"] or "retained" in s["key"]])
        text = "\n".join(C._context_reference_lines(m)[0])
        self.assertIn("ni l'une ni l'autre n'est un gain", text)

    def test_three_windows_are_not_enough_for_a_correlation_to_enter_the_estimate(self) -> None:
        """Constate le 2026-09-21 (session 01a0bb58, import sans le fait de compaction) : un agent retenu dans l'estimation
        sur r = 0,999 avec 3 fenetres. Sans le fait releve, il faut assez de fenetres ; avec lui, le fait decide."""
        def build(windows: int) -> Any:
            main = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
            child = RolloutBuilder(CHILD, parent=ROOT, nickname="Sagan").meta().turn("turn-c")
            for window in range(1, windows + 1):
                send(main, f"call_m{window}", "tache", token(chr(96 + window), 1_000))
                receive(child, "/root", CHILD_PATH, token(chr(96 + window), 1_000), ms=2_000)
                child.usage(90_000, 80_000, 500)
                child.add("compacted", {"message": "SECRET SUMMARY", "window_number": window, "replacement_history": []})
                child.usage(40_000 + 190 * window, 30_000, 100)
            main.usage(900, 800, 10)
            v = self._view(main, child)
            for m in v.markers:                                               # * import anterieur : le fait n'a pas ete ecrit
                m.meta.pop("replacement_agent_messages", None)
            self.last_view = v
            return agent_exchanges(v, self.cfg)
        ex = build(3)
        est = ex["retained_context_estimate"]
        self.assertEqual((est["agents"], est["tokens"], est["agents_with_too_few_windows"]), ([], 0, [CHILD]))   # * avant : [CHILD], 760
        # * un champ non importe n'est pas un zero : None, et le rapport dit quelle methode reste disponible
        row = next(r for r in ex["resident_messages"] if r["agent"] == CHILD)
        self.assertEqual((row["compactions"], row["compactions_observed"], row["compactions_with_fact"]), (None, 3, 0))
        self.assertEqual(est["kept_messages_fact"], {"agents_with_fact": 0, "agents_without_fact": 1, "kept_at_last_compaction": None,
                                                     "received_before_last_compaction": None})
        from agentwatch.reports import compare as C
        from agentwatch.reports.markdown import _render_exchanges
        text = "\n".join(_render_exchanges(ex, {"agent_labels": {"main": "principal", CHILD: "Sagan"}}))
        self.assertIn("NON RELEVES pour Sagan", text)
        self.assertIn("ce n'est pas un zero", text)
        m = C.merge([C.measure(self.last_view, self.cfg)])
        self.assertEqual(m["context"]["exact"]["kept_fact_known_sessions"], 0)
        rows = {r["key"]: r["before"] for r in C._context_rows(m, m, "exact")}
        self.assertIsNone(rows["kept_agent_messages_share"])
        est = build(5)["retained_context_estimate"]
        self.assertEqual((est["agents"], est["agent_basis"]), ([CHILD], {CHILD: "correlation"}))

    def test_a_thread_whose_compaction_keeps_no_agent_message_is_out_of_the_estimate(self) -> None:
        """Le fil principal recoit des messages et sa compaction n'en garde aucun (0 sur 95 releve) : hors estimation,
        quelle que soit la correlation."""
        main = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        child = RolloutBuilder(CHILD, parent=ROOT, nickname="Sagan").meta().turn("turn-c")
        for window in range(1, 7):
            send(child, f"call_c{window}", "/root", token(chr(96 + window), 1_000))
            receive(main, CHILD_PATH, "/root", token(chr(96 + window), 1_000), ms=3_000)
            main.usage(90_000, 80_000, 500)
            main.add("compacted", {"message": "SECRET SUMMARY", "window_number": window,
                                   "replacement_history": [{"type": "message", "role": "developer"}, {"type": "compaction"}]})
            main.usage(40_000 + 190 * window, 30_000, 100)
        child.usage(900, 800, 10)
        ex = agent_exchanges(self._view(main, child), self.cfg)
        row = next(r for r in ex["resident_messages"] if r["agent"] == "main")
        self.assertEqual((row["correlation"], row["compactions"][-1]), (1.0, {"kept_agent_messages": 0, "received_before": 6}))
        self.assertEqual(ex["retained_context_estimate"]["agents"], [])

    # ------------------------------------------------------------------ ressources partagees
    def test_shared_resources_tell_reference_handoff_and_shared_write(self) -> None:
        patch = {"type": "FileChange", "id": "patch-m", "status": "completed",
                 "changes": {"C:\\proj\\Source\\a.cpp": {"type": "update", "unified_diff": "@@ SECRET"}}}
        main = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        main.exec_("call_p", [patch], usage=(20_000, 19_000, 100))
        main.exec_("call_g", [main.cmd("exec-g", "Get-Content -LiteralPath 'Docs/GUIDE.md' -Raw", "g" * 8_000)],
                   usage=(20_200, 19_000, 100))
        main.usage(22_300, 20_000, 100)                                       # * le guide a ajoute 2 000 tokens
        send(main, "call_m1", "tache", token("a", 127))
        child = RolloutBuilder(CHILD, parent=ROOT, nickname="Sagan").meta().turn("turn-c")
        receive(child, "/root", CHILD_PATH, token("a", 127), ms=5_000)
        child.exec_("call_r", [child.cmd("exec-r", "Get-Content -LiteralPath 'Source/a.cpp' -Raw", "c" * 12_000)],
                    usage=(10_000, 9_000, 100))
        child.exec_("call_g2", [child.cmd("exec-g2", "Get-Content -LiteralPath 'Docs/GUIDE.md' -Raw", "g" * 8_000)],
                    usage=(13_100, 9_000, 100))                               # * a.cpp a ajoute 3 000 tokens
        child.exec_("call_p2", [dict(patch, id="patch-c")], usage=(15_200, 13_000, 100))   # * le guide : 2 000
        child.usage(15_400, 15_000, 50)
        res = agent_exchanges(self._view(main, child), self.cfg)["resources"]
        ref = res["shared_reference"]
        self.assertEqual((ref["resources"], ref["reads_by_others"], ref["added_tokens_by_others"]), (1, 1, 2_000))
        self.assertEqual((ref["top"][0]["label"], ref["top"][0]["first_reader"], ref["top"][0]["distinct_contents"]),
                         ("docs/guide.md", "main", 1))
        hand = res["handoff"]
        self.assertEqual(hand["routes"], [{"author": "main", "reader": CHILD, "resources": 1, "reads": 1, "added_tokens": 3_000,
                                           "reads_after_author_message": 1}])
        self.assertTrue(hand["top"][0]["message_between"])
        self.assertTrue(hand["top"][0]["read"]["source"]["line"])             # * preuve : ligne du rollout du lecteur
        self.assertEqual(res["shared_write_total"], 1)
        self.assertEqual([(r["agent"], r["writes"]) for r in res["shared_write"][0]["runs"]], [("main", 1), (CHILD, 1)])

    def test_shared_write_shows_every_writer_even_after_many_writes_by_the_first(self) -> None:
        """Constate le 2026-09-21 (session 01a0bb58) : « Modifie par plusieurs agents : playerprofile.md : Leibniz x12 ».
        La sequence, coupee aux 12 premieres ecritures, ne montrait qu'un agent pour un fichier modifie par deux."""
        def patch(i: str) -> dict[str, Any]:
            return {"type": "FileChange", "id": f"patch-{i}", "status": "completed",
                    "changes": {"C:\\proj\\Docs\\profil.md": {"type": "update", "unified_diff": "@@ SECRET"}}}
        main = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        for i in range(13):
            main.exec_(f"call_p{i}", [patch(f"m{i}")], usage=(20_000 + i, 19_000, 100))
        child = RolloutBuilder(CHILD, parent=ROOT, nickname="Sagan").meta().turn("turn-c")
        child.add("turn_context", {"turn_id": "turn-c", "cwd": "C:\\proj", "model": "gpt-synth"}, ms=60_000)   # * apres le principal
        child.exec_("call_pc", [patch("c")], usage=(10_000, 9_000, 100))
        child.usage(10_200, 10_000, 50)
        v = self._view(main, child)
        w = agent_exchanges(v, self.cfg)["resources"]["shared_write"][0]
        self.assertEqual(w["writes"], {"main": 13, CHILD: 1})
        self.assertEqual([(r["agent"], r["writes"]) for r in w["runs"]], [("main", 13), (CHILD, 1)])      # * avant : main seul
        from agentwatch.reports.markdown import _render_exchanges
        text = "\n".join(_render_exchanges(agent_exchanges(v, self.cfg), {"agent_labels": {"main": "principal", CHILD: "Sagan"}}))
        self.assertIn("principal x13 (#", text)
        self.assertIn("puis Sagan x1 (#", text)

    def test_single_agent_has_no_section(self) -> None:
        main = RolloutBuilder(ROOT).meta().turn("turn-1", "SECRET demande")
        main.exec_("call_w", [main.cmd("exec-w", "rg foo Source", "match")])
        self.assertIsNone(agent_exchanges(self._view(main), self.cfg))

    def test_report_section_states_facts_and_limits(self) -> None:
        from agentwatch.reports.markdown import _render_exchanges
        v = self._view(*self._out_of_order())
        text = "\n".join(_render_exchanges(agent_exchanges(v, self.cfg), {"agent_labels": {"main": "principal", CHILD: "Sagan"}}))
        self.assertIn("## Entre agents", text)
        self.assertIn("2 rapproches ; 1 envoi(s) jamais entre(s)", text)
        self.assertIn("jamais entre : principal -> Sagan", text)
        self.assertIn("semantiquement indetermine", text)
        self.assertIn("ne dit pas « ne travaille pas »", text)
        self.assertIn("ne dit pas que le meme contenu est relaye", text)
        self.assertNotIn("sans travail entre deux", text)


if __name__ == "__main__":
    unittest.main()
