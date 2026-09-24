"""Synthetic historical evidence; no live scripts, clients or project writes."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agentwatch import cli
from agentwatch.reports import inspect_runs as R


class RunViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.projects = self.root / "projects"
        self.source = self.projects / "fixture" / "session.jsonl"
        self.source.parent.mkdir(parents=True)
        self.cfg = {"transcripts": {"claude_projects_dir": str(self.projects)}}
        (self.home / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        self.rows = []
        self.row("assistant", message={"content": [{"type": "tool_use", "id": "runner-source", "name": "Read", "input": {"file_path": "C:/project/run_game_scenario.py"}}]})
        self.result("runner-source", "ROOT = pathlib.Path(__file__).resolve().parents[0]\nout = ROOT / 'Saved' / 'Runs' / args.label")
        self.call("launch", "python run_game_scenario.py --label Run1")
        self.result("launch", "Command running in background with ID: bg1.")
        self.call("watch", "python watch_run.py Saved/Runs/Run1", name="Monitor")
        self.result("watch", "Monitor started (task monitor1, expires in 30m)")
        self.notification("monitor1", "host 9.3 hitch 6719 ms 6719 ms")

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, kind, **kwargs):
        n = len(self.rows) + 1
        self.rows.append({"type": kind, "uuid": f"u{n}", "parentUuid": f"u{n-1}" if n > 1 else None,
                          "cwd": "C:/project", "timestamp": f"2026-09-24T12:00:{n:02}Z", **kwargs})

    def call(self, cid, command, name="Bash"):
        self.row("assistant", message={"content": [{"type": "tool_use", "id": cid, "name": name,
                                                     "input": {"command": command}}]})

    def result(self, cid, text, **kwargs):
        self.row("user", message={"content": [{"type": "tool_result", "tool_use_id": cid,
                                                "content": text, **kwargs}]})

    def notification(self, tid, event, status=None):
        prompt = f"<task-notification><task-id>{tid}</task-id><event>{event}</event>"
        if status:
            prompt += f"<status>{status}</status>"
        prompt += "</task-notification>"
        self.row("attachment", attachment={"type": "queued_command", "prompt": prompt},
                 rendered=[{"content": prompt}], renderedInHumanTurn=[{"content": prompt}])

    def write(self):
        self.source.write_text("".join(json.dumps(r) + "\n" for r in self.rows), encoding="utf-8")

    def read(self, **kwargs):
        self.write()
        return R.analyse(self.cfg, str(self.home), "session", **kwargs)

    def view(self, **kwargs):
        return self.read(run="Run1", **kwargs)["view"]

    def test_discovery_is_bounded_and_preserves_reference(self):
        self.notification("monitor1", "host 10.0 ok " + "x" * 1000)
        data = self.read(limit=1, max_chars=60)
        self.assertEqual(data["total_runs"], 1)
        self.assertEqual(data["runs"][0]["anchor"]["line"], 3)
        self.assertLessEqual(len(data["runs"][0]["preview"]), 60)
        self.assertNotIn("x" * 100, json.dumps(data))

    def test_script_filename_in_git_add_is_not_an_invocation(self):
        self.call("stage", "git add Scripts/watch_run.py \\\n Scripts/t5_view.py \\\n file.txt")
        self.result("stage", "1 file changed")
        self.assertEqual(self.read()["total_runs"], 1)

    def test_command_documentation_does_not_create_phantom_run(self):
        self.call("echo", 'echo "python run_game_scenario.py --label Phantom"')
        self.result("echo", "ok")
        self.call("rg", 'rg "python watch_run.py Saved/Runs/Phantom" README.md')
        self.result("rg", "ok")
        self.assertEqual(self.read()["total_runs"], 1)

    def test_duplicate_launch_copy_keeps_one_run_and_notification_call_link(self):
        import copy
        self.rows.extend(copy.deepcopy(self.rows[2:4]))
        data = self.read()
        self.assertEqual(data["total_runs"], 1)
        notification = next(o for o in self.view()["timeline"] if o["kind"] == "notification")
        self.assertEqual(notification["call_id"], "watch")
        self.assertEqual(notification["call_source"]["line"], 5)

    def test_no_timestamp_is_available_at_physical_frontier_only(self):
        self.rows[6].pop("timestamp")
        physical = self.view(thread="session", through_line=7, fields=["hitch_ms"])
        self.assertEqual(physical["missing"], [])
        self.assertIsNone(physical["fields"]["hitch_ms"]["roles"]["host"]["latest_recorded"]["observed_at"])
        timed = self.view(until=R.I._ns("2026-09-24T13:00:00Z"), fields=["hitch_ms"])
        self.assertEqual(timed["missing"], ["hitch_ms"])

    def test_windows_path_selector_and_compact_answer(self):
        data = self.read(run=r"C:\project\Saved\Runs\Run1", fields=["hitch_ms"])
        compact = R.compact_answer(data)
        field = compact["fields"]["hitch_ms"]["roles"]["host"]
        self.assertEqual(field["max_visible"]["value"], 6719)
        self.assertEqual(compact["evidence"][field["max_visible"]["evidence"]]["line"], 7)

    def test_rounded_time_different_stream_phases_are_not_conflicting(self):
        self.call("read", "python t5_view.py Run1")
        self.result("read", "======================== host\n43.2 STREAM hote_admis MB=12790\n43.2 STREAM observation MB=12788")
        data = self.read(run="Run1", fields=["stream.MB"])
        field = R.compact_answer(data)["fields"]["stream.MB"]["roles"]["host"]
        self.assertEqual(field["conflicting_keys"], 0)
        self.assertEqual(field["min_visible"]["phase"], "observation")
        self.assertEqual(field["max_visible"]["phase"], "hote_admis")

    def test_watcher_end_alone_is_not_scenario_end_or_process_exit(self):
        self.notification("monitor1", "run finished", status="completed")
        data = self.view(fields=["watcher_end", "run_end", "process_status", "process_exit"])
        self.assertEqual(data["missing"], ["run_end", "process_exit"])
        self.assertIn("watcher", data["fields"]["process_status"]["roles"])

    def test_explicit_process_role_is_not_replaced_by_wrapper_scope(self):
        self.call("read", "python t5_view.py Run1")
        self.result("read", '{"role":"client","exit_code":7,"ended":false}')
        data = self.view(fields=["process_exit"])
        self.assertEqual(data["fields"]["process_exit"]["roles"]["client"]["latest_recorded"]["value"], 7)

    def test_discovery_preview_and_count_honor_filters(self):
        data = self.read(kinds="resultat", source_line=6)
        self.assertEqual(data["runs"][0]["observations"], 1)
        self.assertIn("Monitor started", data["runs"][0]["preview"])
        self.assertNotIn("6719", data["runs"][0]["preview"])

    def test_missing_call_ids_never_join_two_launches_or_results(self):
        self.call(None, "python run_game_scenario.py --label MissingOne")
        self.result(None, "host 11.0 END 0 echec(s)")
        self.call(None, "python run_game_scenario.py --label MissingTwo")
        self.result(None, "host 11.0 END 2 echec(s)")
        for label in ("MissingOne", "MissingTwo"):
            data = self.read(run=label, fields=["tests_failed"])
            self.assertEqual(data["view"]["missing"], ["tests_failed"])
        self.assertEqual(self.read()["unresolved_link_count"], 2)

    def test_notification_missing_metrics_and_later_result(self):
        self.call("read", "python t5_view.py Run1")
        self.result("read", "======================== host\n10.0 STREAM observation MB=5014 lv=35")
        before = self.view(thread="session", through_line=7, fields=["hitch_ms", "stream.MB"])
        self.assertEqual(before["missing"], ["stream.MB"])
        after = self.view(fields=["hitch_ms", "stream.MB"])
        self.assertEqual(after["fields"]["stream.MB"]["roles"]["host"]["latest_recorded"]["value"], 5014)
        self.assertEqual(after["missing"], [])
        self.assertIsNone(after["timeline"][2]["api_consumption_proven"])
        self.assertEqual(after["timeline"][2]["facts"][0]["field"], "hitch_ms")
        self.assertEqual(after["timeline"][2]["facts"][0]["value"], 6719)

    def test_time_reversal_uses_physical_frontier_and_preserves_latest_version(self):
        self.call("read", "python t5_view.py Run1")
        self.result("read", "======================== host\n10.0 STREAM observation MB=5014")
        self.call("later", "python t5_view.py Run1")
        self.result("later", "======================== host\n10.0 STREAM observation MB=6014")
        self.rows[-1]["timestamp"] = self.rows[6]["timestamp"]
        early = self.view(thread="session", through_line=9, fields=["stream.MB"])
        self.assertEqual(early["fields"]["stream.MB"]["roles"]["host"]["latest_recorded"]["value"], 5014)
        late = self.view(fields=["stream.MB"])
        field = late["fields"]["stream.MB"]["roles"]["host"]
        self.assertEqual(field["latest_recorded"]["value"], 6014)
        self.assertEqual(field["conflicting_keys"], 1)
        selected = self.view(fields=["stream.MB"], source_line=9)
        self.assertEqual(selected["fields"]["stream.MB"]["roles"]["host"]["conflicting_keys"], 0)

    def test_distinct_runs_same_text_and_old_notification(self):
        self.call("launch2", "cd C:/other && python run_game_scenario.py --label Run1")
        self.call("watch2", "cd C:/other && python watch_run.py Saved/Runs/Run1", name="Monitor")
        self.result("watch2", "Monitor started (task monitor2, expires in 30m)")
        self.notification("monitor2", "host 9.3 hitch 6719 ms 6719 ms")
        self.notification("monitor1", "host 11.0 hitch 9999 ms 9999 ms")
        with self.assertRaisesRegex(ValueError, "ambigu"):
            self.view()
        old = self.read(run="monitor1", fields=["hitch_ms"])["view"]
        new = self.read(run="monitor2", fields=["hitch_ms"])["view"]
        self.assertEqual(old["fields"]["hitch_ms"]["roles"]["host"]["max_visible"]["value"], 9999)
        self.assertEqual(new["fields"]["hitch_ms"]["roles"]["host"]["max_visible"]["value"], 6719)

    def test_failed_tool_and_pending_receipt_never_supply_run_facts(self):
        self.call("read", "python t5_view.py Run1")
        self.result("read", "<tool_use_error>blocked: host 11.0 END 0 echec(s)</tool_use_error>", is_error=True)
        result = self.view(fields=["tests_failed", "process_exit", "run_end"])
        self.assertEqual(len(result["missing"]), 3)
        self.assertTrue(result["timeline"][-1]["is_error"])

    def test_end_failure_count_not_process_success(self):
        self.notification("monitor1", "host 11.0 end 2 echec(s)\nrun finished", status="completed")
        data = self.view(fields=["tests_failed", "process_status", "process_exit", "business_accepted"])
        self.assertEqual(data["fields"]["tests_failed"]["roles"]["host"]["latest_recorded"]["value"], 2)
        self.assertIn("watcher", data["fields"]["process_status"]["roles"])
        self.assertEqual(data["missing"], ["process_exit", "business_accepted"])

    def test_repeated_read_after_compaction_not_assumed_still_available(self):
        self.row("system", subtype="compact_boundary", compactMetadata={"preTokens": 100, "postTokens": 20})
        self.call("reread", "cat /tmp/tasks/monitor1.output")
        self.result("reread", "host 9.3 hitch 6719 ms 6719 ms")
        data = self.view(fields=["hitch_ms"])
        self.assertEqual(data["timeline"][-1]["categories"], {"relecture_apres_compaction_ou_contexte_indetermine": 1})

    def test_partial_newer_and_missing_proof(self):
        self.call("read", "python t5_view.py Run1 | head -40")
        self.result("read", "======================== host\n10.0 STREAM observation MB=5014\n<\u0074runcated>")
        self.call("read2", "python t5_view.py Run1 | tail -8")
        self.result("read2", "======================== host\n20.0 STREAM observation MB=5020")
        self.notification("unrelated", "host 100.0 end 0 echec(s)")
        data = self.view(fields=["stream.MB", "run_end"])
        self.assertTrue(data["timeline"][-1]["partial"])
        self.assertEqual(data["timeline"][-1]["categories"], {"etat_plus_recent": 1})
        self.assertEqual(data["missing"], ["run_end"])
        self.assertEqual(self.read()["unresolved_link_count"], 1)

    def test_same_path_relaunch_does_not_silently_join_label_reads(self):
        self.call("launch2", "python run_game_scenario.py --label Run1")
        self.call("uncertain", "python t5_view.py Run1")
        self.result("uncertain", "host 100.0 end 0 echec(s)")
        result = self.read(run="monitor1", fields=["run_end"])
        self.assertEqual(result["view"]["missing"], ["run_end"])
        self.assertEqual(result["unresolved_link_count"], 1)

    def test_filters_masking_full_result_reference_and_source_protection(self):
        self.call("read", "python t5_view.py Run1")
        self.result("read", "======================== host\n10.0 STREAM observation MB=5014\npassword=hunter2hunter2")
        data = self.view(kinds="resultat", source_line=9, fields=["stream.MB"])
        self.assertEqual(len(data["timeline"]), 1)
        self.assertEqual(data["timeline"][0]["full_result"]["source_line"], 9)
        self.assertNotIn("hunter2hunter2", json.dumps(data))
        self.write()
        before = self.source.read_bytes()
        args = ["--home", str(self.home), "inspect", "--client", "claude-code", "--session", "session", "--run", "Run1"]
        with redirect_stdout(io.StringIO()), patch.object(cli, "_auto_import_rollouts") as imported:
            self.assertEqual(cli.main(args + ["--field", "stream.MB", "--format", "jsonl", "--out", str(self.root / "out.jsonl")]), 0)
            imported.assert_not_called()
            with self.assertRaisesRegex(SystemExit, "journal source"):
                cli.main(args + ["--out", str(self.source)])
        self.assertEqual(before, self.source.read_bytes())

    def test_codex_rejected_before_import_and_frontier_requires_thread(self):
        with patch.object(cli, "_auto_import_rollouts") as imported, self.assertRaises(SystemExit):
            cli.main(["inspect", "--client", "codex", "--session", "any", "--list-runs"])
        imported.assert_not_called()
        with self.assertRaisesRegex(ValueError, "thread"):
            self.view(through_line=7)

    def test_start_by_call_id_and_unresolved_call_remains_inspectable(self):
        self.assertEqual(self.read(run="launch")["view"]["label"], "Run1")
        self.call("shell", "sh C:/unknown/route.sh route Run1")
        self.result("shell", "Command running in background with ID: unresolved-task.")
        self.call("read-shell", "cat /tmp/tasks/unresolved-task.output")
        self.result("read-shell", "host 11.0 END 0 echec(s)")
        diagnostic = self.read(run="shell", fields=["run_end"])
        self.assertEqual(diagnostic["schema"], "agentwatch.run-call.v1")
        self.assertEqual(diagnostic["call"]["fields"]["run_end"]["status"], "not_attributed_to_a_run")
        self.assertTrue(any(e["call_id"] == "read-shell" for e in diagnostic["call"]["evidence"]))
        self.assertEqual(R.compact_answer(diagnostic), diagnostic)
        self.assertEqual(self.read()["launcher_status_counts"], {"partial": 2})

    def test_dynamic_launch_is_counted_and_call_filters_are_honored(self):
        self.call("dynamic", 'python run_game_scenario.py --label "$LABEL"')
        self.result("dynamic", "opaque")
        data = self.read(kinds="appel", source_line=len(self.rows)-1)
        self.assertEqual(data["launcher_count"], 1)
        self.assertEqual(data["launchers"][0]["call_id"], "dynamic")
        self.assertEqual(data["launchers"][0]["status"], "not_demonstrable")

    def test_call_id_and_run_label_collision_is_rejected(self):
        self.call("other", "python run_game_scenario.py --label launch")
        with self.assertRaisesRegex(ValueError, "ambigu"):
            self.read(run="launch")
        self.assertEqual(self.read(run="session:launch")["view"]["label"], "Run1")

    def test_future_result_reference_is_not_exposed_in_historical_metadata(self):
        self.call("pending", "sh C:/unknown/script.sh route Run1")
        boundary = len(self.rows)
        self.result("pending", "future response")
        data = self.read(run="pending", thread="session", through_line=boundary)
        self.assertNotIn("future response", json.dumps(data))
        self.assertEqual(len(data["call"]["evidence"]), 1)
        events, _, _ = R.load_events(self.cfg, str(self.home), "session", thread="session", through_line=boundary)
        pending = next(e for e in events if e["kind"] == "appel" and e["data"].get("call_id") == "pending")
        self.assertIsNone(pending["data"].get("result_source"))

    def test_future_agent_identity_does_not_change_bounded_thread_selector(self):
        self.write()
        child = self.source.parent / "session" / "subagents" / "agent-child.jsonl"
        child.parent.mkdir(parents=True)
        rows = [{"type":"assistant", "agentId":"early-agent", "timestamp":"2026-09-24T12:00:00Z", "message":{"content":[{"type":"text","text":"before"}]}},
                {"type":"assistant", "agentId":"later-agent", "timestamp":"2026-09-24T12:10:00Z", "message":{"content":[{"type":"text","text":"after"}]}}]
        child.write_text("\n".join(json.dumps(r) for r in rows)+"\n", encoding="utf-8")
        for bounds in ({"through_line":1}, {"until":R.I._ns("2026-09-24T12:05:00Z")}):
            events, _, _ = R.load_events(self.cfg, str(self.home), "session", thread="early-agent", **bounds)
            self.assertEqual({e["thread"] for e in events}, {"early-agent"})


if __name__ == "__main__":
    unittest.main()
