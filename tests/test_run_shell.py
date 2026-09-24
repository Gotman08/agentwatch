import unittest

from agentwatch.collector import privacy as P
from agentwatch.reports import inspect_runs as R
from agentwatch.reports.run_shell import resolve_shell_calls


class ShellResolverTests(unittest.TestCase):
    def setUp(self):
        self.key = b"cycle5-test-key"
        self.line = 1

    def source(self, line=None, block=None, path="session.jsonl"):
        value = {"path": path, "line": line or self.line, "block": block, "ts": None, "ns": None}
        self.line = value["line"] + 1
        return value

    def call(self, call_id, command, *, line=None, block=None, name="Bash", cwd="C:/work", thread="thread-1"):
        return {"kind": "appel", "source": self.source(line, block), "thread": thread, "cwd": cwd,
                "data": {"call_id": call_id, "name": name, "input": {"command": command}}}

    def write(self, call_id, path, content, *, line=None, cwd="C:/work", thread="thread-1"):
        return {"kind": "appel", "source": self.source(line), "thread": thread, "cwd": cwd,
                "data": {"call_id": call_id, "name": "Write",
                         "input": {"file_path": path, "content": content}}}

    def result(self, call_id, text, *, line=None, is_error=None, cwd="C:/work", thread="thread-1"):
        return {"kind": "resultat", "source": self.source(line), "thread": thread, "cwd": cwd,
                "data": {"call_id": call_id, "is_error": is_error, "content": text}}

    def resolve(self, events):
        return resolve_shell_calls(events, self.key, target_for_command=R._target, normalize_path=R._path)

    def wrapper(self, *, label='"$2"', cd="/g/work", case="route"):
        return ("#!/bin/sh\n"
                f"cd {cd} || exit 1\n"
                "EXE=Saved/Items.exe\n"
                "case \"$1\" in\n"
                f" {case})\n"
                f"  python Scripts/run_game_scenario.py --label {label} --exe \"$EXE\"\n"
                "  ;;\n"
                " *)\n"
                "  echo unknown\n"
                "  exit 2\n"
                "  ;;\n"
                "esac\n")

    def established_events(self, *, content=None, launcher=None):
        content = content or self.wrapper()
        launcher = launcher or 'sh "C:/work/run_route.sh" route Pkg5Route01 > C:/work/out.log 2>&1; echo exit; cut -c1-3000 C:/work/out.log'
        return [
            self.write("write-1", "C:/work/run_route.sh", content),
            self.result("write-1", "File created successfully at: C:/work/run_route.sh"),
            self.call("launch-1", launcher),
        ]

    def test_supported_case_wrapper_establishes_target_and_evidence(self):
        events = self.established_events()
        data = self.resolve(events)
        row = next(iter(data.values()))
        self.assertEqual(row["status"], "established")
        self.assertEqual(row["invocation"]["arguments"], ["route", "Pkg5Route01"])
        self.assertEqual(row["invocation"]["script"], "c:/work/run_route.sh")
        expected_target = {
            "workspace": "g:/work", "label": "Pkg5Route01", "artifact": None,
            "mode": "launch", "partial": False, "task_refs": [],
        }
        self.assertEqual({key: row["target"][key] for key in expected_target}, expected_target)
        self.assertEqual(row["evidence"][4]["version_hmac"], P.fingerprint(self.key, self.wrapper()))
        self.assertTrue(any(item.get("role") == "definition_write" for item in row["evidence"]))
        self.assertTrue(any(item.get("role") == "definition_confirmation" for item in row["evidence"]))
        self.assertTrue(any(item.get("kind") == "parameter_mapping" for item in row["evidence"]))
        self.assertTrue(next(item for item in row["evidence"] if item.get("kind") == "definition")["lines"])

    def test_windows_bash_path_normalization(self):
        content = self.wrapper()
        events = [
            self.write("write-1", "/c/work/run.sh", content),
            self.result("write-1", "File created successfully at: /c/work/run.sh"),
            self.call("launch-1", "bash '/c/work/run.sh' route Pkg5Route01", cwd="C:/work"),
        ]
        row = next(iter(self.resolve(events).values()))
        self.assertEqual(row["status"], "established")
        self.assertEqual(row["invocation"]["script"], "c:/work/run.sh")

    def test_callback_runner_script_field_is_preserved(self):
        def target_with_runner(command, cwd):
            target = R._target(command, cwd)
            target["runner_script"] = "g:/work/Scripts/run_game_scenario.py"
            return target

        row = next(iter(resolve_shell_calls(self.established_events(), self.key,
                                             target_for_command=target_with_runner,
                                             normalize_path=R._path).values()))
        self.assertEqual(row["target"]["runner_script"], "g:/work/Scripts/run_game_scenario.py")

    def test_missing_historical_script_is_unresolved(self):
        data = self.resolve([self.call("launch-1", "sh C:/work/run.sh route Pkg5Route01")])
        row = next(iter(data.values()))
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "no_historical_script_version")

    def test_confirmation_after_invocation_does_not_leak_forward(self):
        content = self.wrapper()
        data = self.resolve([
            self.call("launch-1", "sh C:/work/run.sh route Pkg5Route01"),
            self.write("write-1", "C:/work/run.sh", content),
            self.result("write-1", "File created successfully at: C:/work/run.sh"),
        ])
        row = next(iter(data.values()))
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "no_historical_script_version")

    def test_contradictory_write_calls_stay_unresolved(self):
        first, second = self.wrapper(), self.wrapper(label='"$2"\n')
        events = [
            self.write("write-1", "C:/work/run.sh", first),
            self.write("write-1", "C:/work/run.sh", second),
            self.result("write-1", "File created successfully at: C:/work/run.sh"),
            self.call("launch-1", "sh C:/work/run.sh route Pkg5Route01"),
        ]
        row = next(iter(self.resolve(events).values()))
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "historical_write_contradictory")

    def test_overwrite_or_edit_invalidates_old_version(self):
        content = self.wrapper()
        events = self.established_events(content=content)
        events.extend([
            self.call("edit-1", "python -c \"Path('C:/work/run_route.sh').write_text('changed')\"", name="Bash"),
            self.call("launch-2", "sh C:/work/run_route.sh route Pkg5Route02"),
        ])
        rows = self.resolve(events)
        row = list(rows.values())[-1]
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "historical_shell_mutation")

    def test_write_id_conflicting_with_any_other_call_cannot_establish_version(self):
        for after_confirmation in (False, True):
            events = self.established_events()
            position = 2 if after_confirmation else 1
            events.insert(position, self.call("write-1", "echo not-a-write"))
            row = next(iter(self.resolve(events).values()))
            self.assertEqual(row["status"], "unresolved")
            self.assertEqual(row["reason"], "historical_write_contradictory")

    def test_malformed_call_id_does_not_create_write_ownership(self):
        events = self.established_events()
        events[0]["data"]["call_id"] = ["invalid"]
        row = next(iter(self.resolve(events).values()))
        self.assertEqual(row["status"], "unresolved")

    def test_relative_or_heredoc_mutation_invalidates_known_script(self):
        events = self.established_events()
        events.extend([
            self.call("mutate-1", "sed -i 's/route/cold/' scratchpad/run_route.sh"),
            self.call("launch-2", "sh C:/work/run_route.sh route Pkg5Route02"),
        ])
        row = list(self.resolve(events).values())[-1]
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "historical_shell_mutation")

        events = self.established_events()
        events.extend([
            self.call("mutate-1", "python - <<'PY'\nfrom pathlib import Path\nPath('run_route.sh').write_text('changed')\nPY"),
            self.call("launch-2", "sh C:/work/run_route.sh route Pkg5Route02"),
        ])
        row = list(self.resolve(events).values())[-1]
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "historical_shell_mutation")

    def test_dynamic_label_is_unresolved(self):
        content = self.wrapper(label='"$LABEL"')
        row = next(iter(self.resolve(self.established_events(content=content)).values()))
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "dynamic_label")

    def test_dynamic_cd_is_unresolved(self):
        content = self.wrapper(cd='"$ROOT"')
        row = next(iter(self.resolve(self.established_events(content=content)).values()))
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "dynamic_cd")

    def test_quoted_shell_text_is_not_an_invocation(self):
        data = self.resolve([self.call("echo-1", 'echo "sh C:/work/run.sh route Pkg5Route01"')])
        self.assertEqual(data, {})
        data = self.resolve([self.call("echo-2", "echo sh C:/work/run.sh route Pkg5Route01")])
        self.assertEqual(data, {})
        data = self.resolve([self.call("heredoc-1", "python - <<'PY'\nprint('sh C:/work/run.sh route Pkg5Route01')\nPY")])
        self.assertEqual(data, {})

    def test_conditional_shell_command_is_unresolved(self):
        command = 'if sh C:/work/run.sh route Pkg5Route01; then echo ok; fi'
        row = next(iter(self.resolve(self.established_events(launcher=command)).values()))
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "conditional_shell_invocation")

    def test_unsupported_case_and_multiple_invocations_are_visible(self):
        content = self.wrapper(case="route|cold")
        row = next(iter(self.resolve(self.established_events(content=content)).values()))
        self.assertEqual(row["status"], "unresolved")
        self.assertIn(row["reason"], {"unknown_case_arm", "non_literal_case_arm"})

        events = self.established_events()
        events[-1] = self.call("launch-1", "sh C:/work/run_route.sh route Pkg1; sh C:/work/run_route.sh route Pkg2")
        row = next(iter(self.resolve(events).values()))
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "multiple_shell_invocations")


if __name__ == "__main__":
    unittest.main()
