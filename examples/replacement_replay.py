"""Exporte un exemple reproductible, exclusivement synthetique et local.

python -m examples.replacement_replay --out <dossier>
Aucune commande contenue dans les fixtures n'est executee.
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from agentwatch import cli
from agentwatch.config import load_config
from agentwatch.selftest import Synth


def export_example(destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentwatch-replacements-") as tmp:
        home = Path(tmp)
        cfg = load_config(home)
        cfg["rollouts"]["auto_import"] = False
        cfg["transcripts"]["auto_import"] = False
        cfg["health"]["enabled"] = False
        cfg["auto_compact_threshold"] = 0
        cfg["mcp_param_allowlist"].append("source_call_id")
        cfg["replacements"]["reaction_deadline_s"] = 60
        cfg["replacements"]["capabilities"] = [{
            "id": "synthetic-wait", "rule_id": "G.repeated_calls", "strategy": "wait",
            "tool_name": "mcp__cluster__wait_for_job",
            "contract": "Attendre un job avec delai maximal ; retourner identifiant, etat et erreurs.",
            "replacement_calls": 1,
        }]
        (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        s = Synth(home, session_id="replacement-example-synthetic", cfg=cfg)
        s.session_start()
        s.user_prompt()
        s.mcp("cluster", "wait_for_job", {"job_id": 1}, json.dumps({"status": "COMPLETED"}))
        for _ in range(3):
            s.read("settings.toml", "mode = 'demo'\n")
        s.glob("src/*.py", filenames=["src/a.py", "src/b.py", "src/c.py"])
        for letter in "abc":
            s.read(f"src/{letter}.py", f"value = '{letter}'\n")
        for _ in range(2):
            s.bash("python -m unittest", "3 tests passed")
        for command in ("ssh cluster 'squeue'", "ssh cluster 'sinfo'", "ssh cluster 'sacct'"):
            s.bash(command, "synthetic output")
        producer = s.mcp("cluster", "submit_job", {"script": "synthetic-only"}, json.dumps({"job_id": 2}))
        s.response_gap_ms = 20_000
        for i in range(14):
            s.mcp("cluster", "job_status", {"job_id": 2, "source_call_id": producer},
                  json.dumps({"status": "RUNNING" if i < 13 else "COMPLETED"}))
        s.stop()
        for fmt, filename in (("json", "agentwatch-exemple-synthetique.json"),
                              ("markdown", "agentwatch-exemple-synthetique.md")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                result = cli.main(["--home", str(home), "report", "--session", s.session_id, "--format", fmt])
            if result:
                raise RuntimeError(f"report failed: {fmt}: {result}")
            content = buf.getvalue()
            if fmt == "markdown":
                content = "> EXEMPLE SYNTHETIQUE : aucune economie mesuree sur une session reelle.\n\n" + content
            (destination / filename).write_text(content, encoding="utf-8")
        report = json.loads((destination / "agentwatch-exemple-synthetique.json").read_text(encoding="utf-8"))
        return {"synthetic": True, "calls": len(report["calls"]), "findings": len(report["findings"]),
                "rules": sorted({f["rule_id"] for f in report["findings"]}),
                "graph_nodes": len(report["observed_dependency_graph"]["nodes"]),
                "graph_edges": len(report["observed_dependency_graph"]["edges"]),
                "files": ["agentwatch-exemple-synthetique.json", "agentwatch-exemple-synthetique.md"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export_example(args.out), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
