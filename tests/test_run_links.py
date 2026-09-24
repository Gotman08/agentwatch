"""Evidence graph counterexamples, using synthetic recordings only."""
import unittest

from agentwatch.reports import inspect_runs as R


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.call("code", name="Read", file_path="C:/p/Scripts/run_game_scenario.py")
        self.result("code", "ROOT = pathlib.Path(__file__).resolve().parents[1]\nout = ROOT / 'Saved' / 'Runs' / args.label")

    def event(self, kind, data, scope="fixture"):
        n = len(self.events) + 1
        e = {"kind": kind, "data": data, "source": {"path": scope, "line": n, "block": 0, "ns": n, "ts": str(n)},
             "thread": scope, "cwd": "C:/p", "boundary": "b", "boundary_limits": [], "flags": []}
        self.events.append(e)
        return e

    def call(self, cid, command=None, name="Bash", scope="fixture", **inp):
        if command is not None:
            inp["command"] = command
        return self.event("appel", {"call_id": cid, "name": name, "input": inp}, scope)

    def launch(self, cid="launch", label="Run"):
        return self.call(cid, f"python Scripts/run_game_scenario.py --label {label}")

    def result(self, cid, content, scope="fixture"):
        return self.event("resultat", {"call_id": cid, "content": content, "is_error": False}, scope)

    def receipt(self, cid, tid):
        return self.result(cid, f"Command running in background with ID: {tid}.")

    def note(self, tid, cid=None, scope="fixture"):
        text = f"<task-id>{tid}</task-id><event>host 12.0 END 0 echec(s)</event>"
        if cid:
            text += f"<tool-use-id>{cid}</tool-use-id>"
        return self.event("notification", {"attachment": {"prompt": text}}, scope)

    def index(self, end=None):
        return R.build_index(self.events[:end], b"synthetic-key")

    def test_one_launcher_many_tasks_and_many_tasks_one_run(self):
        self.launch()
        self.receipt("launch", "one")
        self.receipt("launch", "two")
        self.call("watch", "python watch_run.py Saved/Runs/Run", name="Monitor")
        self.receipt("watch", "three")
        for tid in ("one", "two", "three"):
            self.note(tid)
            self.call(f"read-{tid}", f"cat /tmp/tasks/{tid}.output")
            self.result(f"read-{tid}", "terminal evidence")
        index = self.index()
        self.assertEqual(len(index["runs"]), 1)
        self.assertEqual(set(index["runs"][0]["task_ids"]), {"one", "two", "three"})
        self.assertEqual(len([o for o in index["runs"][0]["observations"] if o["kind"] == "notification"]), 3)
        self.assertTrue(all(e["from"]["id"] and e["to"]["id"] and e["scope"]["source_file"] for e in index["links"]))

    def test_late_receipt_retroactive_join_has_late_known_from(self):
        self.call("read", "cat /tmp/tasks/late.output")
        self.result("read", "terminal evidence")
        self.launch()
        before = self.index()
        self.assertFalse(before["runs"][0]["observations"])
        receipt = self.receipt("launch", "late")
        after = self.index()
        obs = next(o for o in after["runs"][0]["observations"] if o["call_id"] == "read")
        self.assertEqual(obs["association_known_from"]["line"], receipt["source"]["line"])

    def test_late_call_links_earlier_receipt_only_in_available_prefix(self):
        self.receipt("launch", "late")
        self.note("late")
        self.assertEqual(self.index()["runs"], [])
        call = self.launch()
        after = self.index()
        note = next(o for o in after["runs"][0]["observations"] if o["kind"] == "notification")
        self.assertEqual(note["association_known_from"]["line"], call["source"]["line"])

    def test_conflicting_task_owners_even_same_run_do_not_supply_facts(self):
        self.launch()
        self.receipt("launch", "duplicate")
        self.call("watch", "python watch_run.py Saved/Runs/Run", name="Monitor")
        self.receipt("watch", "duplicate")
        self.note("duplicate", "launch")
        self.call("read", "cat /tmp/tasks/duplicate.output")
        self.result("read", "host 10.0 END 0 echec(s)")
        index = self.index()
        self.assertEqual(len(index["runs"]), 1)
        self.assertFalse(any(o["kind"] == "notification" or o["call_id"] == "read" for o in index["runs"][0]["observations"]))
        self.assertTrue(any(u["status"] == "ambiguous" for u in index["unresolved"]))

    def test_notification_identifiers_disagree(self):
        self.launch("first", "One")
        self.receipt("first", "task-one")
        self.launch("second", "Two")
        self.receipt("second", "task-two")
        self.note("task-one", "second")
        index = self.index()
        self.assertFalse(any(o["kind"] == "notification" for r in index["runs"] for o in r["observations"]))

    def test_full_ids_are_scoped_and_never_join_by_prefix(self):
        self.launch("first")
        self.receipt("first", "task-one")
        self.note("task-on")
        self.note("task-one", scope="other-file")
        index = self.index()
        self.assertFalse(any(o["kind"] == "notification" for o in index["runs"][0]["observations"]))

    def test_unknown_explicit_call_id_cannot_override_known_receipt(self):
        self.launch("first")
        self.receipt("first", "task-one")
        self.note("task-one", "missing-other-call")
        index = self.index()
        self.assertFalse(any(o["kind"] == "notification" for o in index["runs"][0]["observations"]))
        self.assertEqual(index["unresolved"][0]["status"], "ambiguous")

    def test_duplicate_conflicting_call_id_leaves_results_unattached(self):
        self.launch("same", "One")
        self.launch("same", "Two")
        self.receipt("same", "task")
        self.note("task", "same")
        self.assertFalse(any(r["observations"] for r in self.index()["runs"]))

    def test_common_basename_different_artifact_does_not_join(self):
        self.launch()
        self.call("watch", "python watch_run.py Other/Runs/Run", name="Monitor")
        self.receipt("watch", "monitor")
        index = self.index()
        self.assertEqual(len(index["runs"]), 2)
        self.assertEqual(index["runs"][0]["task_ids"], [])

    def test_missing_runner_mapping_does_not_join_by_label(self):
        self.events = []
        self.launch()
        self.call("watch", "python watch_run.py Saved/Runs/Run", name="Monitor")
        self.assertEqual(len(self.index()["runs"]), 2)

    def test_watch_bare_path_is_not_a_label_only_join(self):
        self.launch()
        self.call("watch", "python watch_run.py Run", name="Monitor")
        self.assertEqual(len(self.index()["runs"]), 2)

    def test_same_file_different_threads_do_not_share_ids_or_tasks(self):
        self.launch("same")
        self.receipt("same", "task")
        other = self.launch("same")
        other["thread"] = "other"
        receipt = self.receipt("same", "task")
        receipt["thread"] = "other"
        note = self.note("task", "same")
        note["thread"] = "other"
        index = self.index()
        self.assertEqual(len(index["runs"]), 2)
        self.assertFalse(any(o["kind"] == "notification" for o in index["runs"][0]["observations"]))
        self.assertEqual(len([o for o in index["runs"][1]["observations"] if o["kind"] == "notification"]), 1)

    def test_same_artifact_relaunch_remains_ambiguous_for_new_read(self):
        self.launch("one")
        self.launch("two")
        self.call("read", "python t5_view.py Saved/Runs/Run")
        self.result("read", "host 12.0 END 0 echec(s)")
        index = self.index()
        self.assertFalse(any(r["observations"] for r in index["runs"]))
        self.assertEqual(index["unresolved"][0]["status"], "ambiguous")

    def test_heredoc_and_quoted_command_are_not_launches(self):
        self.call("write", "python - <<'PY'\ns='python run_game_scenario.py --label Fake'\nPY\necho done")
        self.call("quote", 'echo "python run_game_scenario.py --label Fake"')
        self.assertEqual(self.index()["runs"], [])

    def test_conditional_python_invocation_remains_an_unresolved_candidate(self):
        self.call("conditional", "false && python Scripts/run_game_scenario.py --label Run")
        self.receipt("conditional", "parent-task")
        index = self.index()
        self.assertEqual(index["runs"], [])
        self.assertEqual(R.launcher_states(index)[0]["status"], "partial")
        self.assertEqual(index["unresolved"][0]["reason"], "conditional_python_invocation")

    def test_conditional_watch_and_conditional_cd_never_anchor_runs(self):
        for command in ("false && python watch_run.py Saved/Runs/Run",
                        "false && python t5_view.py Saved/Runs/Run",
                        "true || cd C:/p && python Scripts/run_game_scenario.py --label Run",
                        "cd C:/elsewhere & python Scripts/run_game_scenario.py --label Run",
                        "cd C:/elsewhere | python Scripts/run_game_scenario.py --label Run"):
            self.events = []
            self.call("conditional", command)
            self.assertEqual(self.index()["runs"], [])
            self.assertTrue(self.index()["unresolved"])

    def test_nested_unparsed_terminal_proof_remains_accessible(self):
        self.launch()
        self.receipt("launch", "task")
        self.call("read", "cat /tmp/tasks/task.output | cut -c1-1500")
        self.result("read", 'exit 1\n{"host":{"exit_code":9,"ended":false}}')
        index = self.index()
        view = R.read_run(index["runs"][0], fields=["run_end", "process_exit"])
        self.assertEqual(view["missing"], ["run_end", "process_exit"])
        self.assertEqual(view["readings"][-1]["classification"], "preuve_non_interpretee_a_examiner")
        self.assertTrue(view["timeline"][-1]["partial"])
        self.assertIsNone(view["readings"][-1]["usefulness_judgement"])

    def test_receipt_looking_view_payload_is_not_a_transport_receipt(self):
        self.launch()
        self.call("read", "python t5_view.py Run")
        self.result("read", "Command running in background with ID: fake.")
        self.note("fake")
        index = self.index()
        self.assertEqual(index["runs"][0]["task_ids"], [])
        self.assertFalse(any(o["kind"] == "notification" for o in index["runs"][0]["observations"]))
        # Explicit transport metadata remains independent of command recognition.
        self.events[-2]["background_task_id"] = "real"
        self.assertIn("real", self.index()["runs"][0]["task_ids"])


if __name__ == "__main__":
    unittest.main()
