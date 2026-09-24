"""Synthetic evidence tests for the recorded runner-to-artifact resolver."""
import unittest

from agentwatch.collector import privacy as P
from agentwatch.reports import inspect_runs as R
from agentwatch.reports.run_artifacts import resolve_run_artifacts


class RunArtifactResolverTests(unittest.TestCase):
    def setUp(self):
        self.key = b"cycle5-artifact-key"
        self.transcript = "C:/journal/session.jsonl"
        self.script = "g:/unrealengine/unearthed-nyk175/scripts/map/navigation/run_game_scenario.py"
        self.cwd = "G:\\UnrealEngine\\Unearthed"
        self.line = 1

    def source(self, line=None, path=None, block=None):
        return {"path": path or self.transcript, "line": line or self.line,
                "block": block, "ts": None, "ns": None}

    def call(self, call_id, name, input_data, *, line=None, path=None, thread="t1", cwd=None):
        return {"kind": "appel", "source": self.source(line, path), "thread": thread,
                "cwd": self.cwd if cwd is None else cwd,
                "data": {"call_id": call_id, "name": name, "input": input_data}}

    def result(self, call_id, content, *, line=None, path=None, thread="t1", is_error=None,
               partial=None, truncated=None):
        data = {"call_id": call_id, "content": content, "is_error": is_error}
        if partial is not None:
            data["partial"] = partial
        if truncated is not None:
            data["truncated"] = truncated
        return {"kind": "resultat", "source": self.source(line, path), "thread": thread,
                "cwd": self.cwd, "data": data}

    def resolver(self, events):
        return resolve_run_artifacts(events, self.key, normalize_path=R._path, text_of=R._text)

    def route_fragments(self):
        root = "ROOT = pathlib.Path(__file__).resolve().parents[3]\n"
        out = "out = ROOT / 'Saved' / 'NYK175' / 'Runs' / args.label\n"
        events = [
            self.call("read-root", "Bash", {
                "command": "cd /g/UnrealEngine/Unearthed-NYK175 && sed -n 1,60p Scripts/Map/Navigation/run_game_scenario.py"
            }, line=211),
            self.result("read-root", root, line=213),
            self.call("read-out", "Bash", {
                "command": "cd /g/UnrealEngine/Unearthed-NYK175 && sed -n 80,125p Scripts/Map/Navigation/run_game_scenario.py"
            }, line=328),
            self.result("read-out", "    " + out, line=329),
        ]
        return events, root, out

    def test_route01_joins_exact_fragments_and_derives_artifact(self):
        events, root, out = self.route_fragments()
        launch = self.source(607)
        proof = self.resolver(events).resolve(self.script, "Pkg5Route01", launch)

        self.assertIsNotNone(proof)
        self.assertEqual(proof["artifact"], "g:/unrealengine/unearthed-nyk175/saved/nyk175/runs/pkg5route01")
        self.assertEqual(proof["rule"], "recorded_python_root_and_out_args_label")
        self.assertEqual(proof["version"], P.fingerprint(self.key, root + "\n" + "    " + out))
        self.assertIn("External edits", " ".join(proof["limits"]))
        self.assertIn("partial", " ".join(proof["limits"]))
        self.assertEqual({item["line"] for item in proof["sources"]}, {211, 213, 328, 329, 607})

    def test_full_read_supports_literal_parent_zero_and_one(self):
        content = (
            "import pathlib\n"
            "ROOT=pathlib.Path(__file__).resolve().parents[0]\n"
            "out = ROOT/'Saved'/'Runs'/args.label\n"
        )
        events = [
            self.call("read", "Read", {"file_path": "G:/UnrealEngine/Unearthed-NYK175/Scripts/runner.py"}, line=10),
            self.result("read", content, line=11),
        ]
        script = "g:/unrealengine/unearthed-nyk175/scripts/runner.py"
        proof = self.resolver(events).resolve(script, "Full01", self.source(20))
        self.assertIsNotNone(proof)
        self.assertEqual(proof["artifact"], "g:/unrealengine/unearthed-nyk175/scripts/saved/runs/full01")

        events[1]["data"]["partial"] = True
        partial_proof = self.resolver(events).resolve(script, "FullPartial01", self.source(20))
        self.assertIsNotNone(partial_proof)
        self.assertIn("partial", " ".join(partial_proof["limits"]))

    def test_no_evidence_does_not_invent_artifact(self):
        proof = self.resolver([]).resolve(self.script, "Missing01", self.source(20))
        self.assertIsNone(proof)

    def test_later_result_is_outside_physical_frontier(self):
        events, _, _ = self.route_fragments()
        events[1]["source"]["line"] = 613
        events[3]["source"]["line"] = 614
        self.assertIsNone(self.resolver(events).resolve(self.script, "Later01", self.source(607)))

    def test_different_transcript_path_and_thread_do_not_join(self):
        events, _, _ = self.route_fragments()
        events[0]["source"]["path"] = "C:/other/session.jsonl"
        events[1]["source"]["path"] = "C:/other/session.jsonl"
        self.assertIsNone(self.resolver(events).resolve(self.script, "Other01", self.source(607)))

        events, _, _ = self.route_fragments()
        events[0]["thread"] = events[1]["thread"] = "other-thread"
        events.append(self.call("launch", "Bash", {"command": "true"}, line=607, thread="t1"))
        self.assertIsNone(self.resolver(events).resolve(self.script, "Thread01", self.source(607)))

    def test_dynamic_expressions_are_unresolved(self):
        content = "ROOT = pathlib.Path(__file__).parent\nout = ROOT / Path(args.label)\n"
        events = [
            self.call("read", "Read", {"file_path": self.script}, line=10),
            self.result("read", content, line=11),
        ]
        self.assertIsNone(self.resolver(events).resolve(self.script, "Dynamic01", self.source(20)))

    def test_truncated_or_error_result_is_unresolved(self):
        content = "ROOT = pathlib.Path(__file__).resolve().parents[3]\nout = ROOT/'Saved'/'Runs'/args.label\n"
        events = [
            self.call("read", "Read", {"file_path": self.script}, line=10),
            self.result("read", content + "<truncated>", line=11),
        ]
        self.assertIsNone(self.resolver(events).resolve(self.script, "Truncated01", self.source(20)))

        events = [
            self.call("read", "Read", {"file_path": self.script}, line=10),
            self.result("read", content, line=11, is_error=True),
        ]
        self.assertIsNone(self.resolver(events).resolve(self.script, "Error01", self.source(20)))

    def test_recorded_mutation_invalidates_mapping(self):
        events, _, _ = self.route_fragments()
        events.append(self.call("edit", "Edit", {"file_path": self.script, "old_string": "x", "new_string": "y"}, line=400))
        self.assertIsNone(self.resolver(events).resolve(self.script, "Mutated01", self.source(607)))

        events, _, _ = self.route_fragments()
        events.append(self.call("sed", "Bash", {
            "command": "cd /g/UnrealEngine/Unearthed-NYK175 && sed -i 's/parents\\[3\\]/parents[2]/' Scripts/Map/Navigation/run_game_scenario.py"
        }, line=401))
        self.assertIsNone(self.resolver(events).resolve(self.script, "Mutated02", self.source(607)))

        events, _, _ = self.route_fragments()
        events.append(self.call("heredoc", "Bash", {
            "command": (
                "cd /g/UnrealEngine/Unearthed-NYK175 && python - <<'PY'\n"
                "from pathlib import Path\n"
                "Path('Scripts/Map/Navigation/run_game_scenario.py').write_text('changed')\n"
                "PY"
            )
        }, line=402))
        self.assertIsNone(self.resolver(events).resolve(self.script, "Mutated03", self.source(607)))

        events, _, _ = self.route_fragments()
        events.append(self.call("heredoc-cwd", "Bash", {
            "command": (
                "cd /g/UnrealEngine/Unearthed-NYK175 && python - <<'PY'\n"
                "from pathlib import Path\n"
                "Path('Scripts/Map/Navigation/run_game_scenario.py').write_text('changed')\n"
                "PY\n"
                "cd /g/elsewhere"
            )
        }, line=403))
        self.assertIsNone(self.resolver(events).resolve(self.script, "Mutated04", self.source(607)))

        events, _, _ = self.route_fragments()
        events.append(self.call("redirect", "Bash", {
            "command": "cd /g/UnrealEngine/Unearthed-NYK175 && echo changed > Scripts/Map/Navigation/run_game_scenario.py"
        }, line=404))
        self.assertIsNone(self.resolver(events).resolve(self.script, "Mutated05", self.source(607)))

    def test_unrelated_compound_mutation_does_not_invalidate_runner(self):
        events, _, _ = self.route_fragments()
        events.append(self.call("launch", "Bash", {
            "command": (
                "cd /g/UnrealEngine/Unearthed-NYK175 && "
                "python -c \"Path('Scripts/other.txt').write_text('x')\" && "
                "python Scripts/Map/Navigation/run_game_scenario.py --label Compound01"
            )
        }, line=400))
        proof = self.resolver(events).resolve(self.script, "Compound01", self.source(400))
        self.assertIsNotNone(proof)
        self.assertEqual(proof["artifact"], "g:/unrealengine/unearthed-nyk175/saved/nyk175/runs/compound01")

    def test_quoted_commands_and_docstrings_are_not_definitions(self):
        events = [
            self.call("echo", "Bash", {"command": 'echo "cat Scripts/Map/Navigation/run_game_scenario.py"'}, line=10),
            self.result("echo", "ROOT = pathlib.Path(__file__).resolve().parents[3]\nout = ROOT/'Saved'/'Runs'/args.label", line=11),
        ]
        self.assertIsNone(self.resolver(events).resolve(self.script, "Quoted01", self.source(20)))

        content = '"""Example\\nROOT = pathlib.Path(__file__).resolve().parents[3]\\nout = ROOT / \'Saved\' / \'Runs\' / args.label\\n"""\\n'
        events = [
            self.call("read", "Read", {"file_path": self.script}, line=10),
            self.result("read", content, line=11),
        ]
        self.assertIsNone(self.resolver(events).resolve(self.script, "Docstring01", self.source(20)))

    def test_same_basename_different_path_does_not_match(self):
        content = "ROOT = pathlib.Path(__file__).resolve().parents[3]\nout = ROOT/'Saved'/'Runs'/args.label\n"
        events = [
            self.call("read", "Read", {"file_path": "g:/other/run_game_scenario.py"}, line=10),
            self.result("read", content, line=11),
        ]
        self.assertIsNone(self.resolver(events).resolve(self.script, "Basename01", self.source(20)))

    def test_conflicting_reused_call_id_cannot_fabricate_fragment_pair(self):
        content = "ROOT = pathlib.Path(__file__).resolve().parents[3]\nout = ROOT/'Saved'/'Runs'/args.label\n"
        events = [
            self.call("reused", "Read", {"file_path": self.script}, line=10),
            self.call("reused", "Read", {"file_path": "g:/other/run_game_scenario.py", "offset": 1}, line=11),
            self.result("reused", content, line=12),
        ]
        self.assertIsNone(self.resolver(events).resolve(self.script, "Conflict01", self.source(20)))

        events = [
            self.call("reused", "Bash", {"command": "echo hi"}, line=10),
            self.call("reused", "Read", {"file_path": self.script}, line=11),
            self.result("reused", content, line=12),
        ]
        self.assertIsNone(self.resolver(events).resolve(self.script, "Conflict02", self.source(20)))


if __name__ == "__main__":
    unittest.main()
