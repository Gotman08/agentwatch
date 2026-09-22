"""Reglages de fil (modele, effort, niveau de service) dans compare : releves par periode depuis les marqueurs `settings`,
changements de niveau comptes, ratios de quota declares non comparables entre niveaux differents ; une mesure anterieure
a ce releve reste « non releve », jamais un zero ni une valeur inventee. Rollout SYNTHETIQUE."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentwatch import cli
from agentwatch.collector import rollouts as R
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core.session import load_session
from agentwatch.core.timeslice import parse_when, slice_view
from agentwatch.reports import compare as CMP
from tests.test_rollouts import ROOT, RolloutBuilder

NOON = "2026-09-19T12:00:00Z"


def settings_event(model: str, tier: str, effort: str = "max") -> dict:
    return {"type": "thread_settings_applied", "thread_settings": {"model": model, "model_provider_id": "openai", "service_tier": tier,
                                                                   "reasoning_effort": effort, "approval_policy": "never"}}


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, sessions = root / "home", root / "sessions"
        day = sessions / "2026" / "09" / "19"
        day.mkdir(parents=True)
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"health": {"codex_sessions_dir": str(sessions)},
                                                           "rollouts": {"background_priority": False}}), encoding="utf-8")
        self.cfg = load_config(self.home)
        self.store = EventStore(self.home, self.cfg)
        b = RolloutBuilder(ROOT).meta()
        b.add("event_msg", settings_event("gpt-6-astra", "default"))            # * 10 h : niveau standard
        b.turn("turn-1", "Lance le build.")
        b.exec_("call_a", [b.cmd("exec-a", "git status --short", " M a.cpp")])
        b.end_turn("turn-1")
        b.t = b.t.replace(hour=15)
        b.add("event_msg", settings_event("gpt-6-astra", "priority"))           # * 15 h : passage en prioritaire
        b.add("event_msg", settings_event("gpt-6-astra", "priority"))           # * reecrit a l'identique : pas un changement
        b.turn("turn-2", "Verifie.")
        b.exec_("call_b", [b.cmd("exec-b", "git status --short", " M b.cpp")])
        b.end_turn("turn-2")
        p = day / f"rollout-2026-09-19T10-00-00-{ROOT}.jsonl"
        p.write_text(b.text(), encoding="utf-8", newline="\n")
        R.import_rollouts(self.store, self.cfg, [str(p)])
        self.view = load_session(self.store, "codex", ROOT, self.cfg)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cli(self, *args: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--home", str(self.home), *args]), 0)
        return buf.getvalue()

    def test_settings_are_measured_per_period_and_merged(self) -> None:
        m = CMP.measure(self.view, self.cfg)
        s = m["settings"]
        self.assertTrue(s["known"])
        self.assertEqual([(c["model"], c["effort"], c["service_tier"], c["count"]) for c in s["configs"]],
                         [("gpt-6-astra", "max", "default", 1), ("gpt-6-astra", "max", "priority", 2)])
        self.assertEqual(s["tier_changes"], 1)                                   # * default -> priority ; priority -> priority ne compte pas
        morning = CMP.measure(slice_view(self.view, None, parse_when(NOON)), self.cfg)
        afternoon = CMP.measure(slice_view(self.view, parse_when(NOON), None), self.cfg)
        self.assertEqual([c["service_tier"] for c in morning["settings"]["configs"]], ["default"])
        self.assertEqual([c["service_tier"] for c in afternoon["settings"]["configs"]], ["priority"])
        self.assertEqual((morning["settings"]["tier_changes"], afternoon["settings"]["tier_changes"]), (0, 0))
        merged = CMP.merge([morning, afternoon])["settings"]
        self.assertEqual([(c["service_tier"], c["count"]) for c in merged["configs"]], [("default", 1), ("priority", 2)])
        self.assertEqual(merged["tier_changes"], 0)                              # * la somme des tranches ne voit pas le passage entre elles
        self.assertEqual(CMP.merge([m])["settings"]["tier_changes"], 1)

    def test_compare_says_when_service_tiers_differ_or_are_not_recorded(self) -> None:
        morning = CMP.measure(slice_view(self.view, None, parse_when(NOON)), self.cfg)
        afternoon = CMP.measure(slice_view(self.view, parse_when(NOON), None), self.cfg)
        sc = CMP.compare(morning, afternoon)["settings"]
        self.assertEqual((sc["service_tier_status"], sc["tiers_before"], sc["tiers_after"]), ("differents", ["default"], ["priority"]))
        self.assertIn("n'en est pas le multiplicateur", sc["quota_limit"])
        self.assertEqual(CMP.compare(morning, morning)["settings"]["service_tier_status"], "identique")
        old = dict(morning)
        del old["settings"]                                                      # * reference enregistree avant ce releve
        sc = CMP.compare(old, afternoon)["settings"]
        self.assertEqual(sc["service_tier_status"], "non releve")
        text = chr(10).join(CMP.settings_lines(sc))
        self.assertIn("avant : non releve (mesure anterieure a ce releve)", text)
        self.assertIn("apres : gpt-6-astra / max / priority (2 releve(s)", text)
        self.assertIn("- Niveau de service : non releve. Limite : les points de quota par requete ne se comparent qu'a niveau de service identique.", text)
        text = chr(10).join(CMP.settings_lines(CMP.compare(morning, afternoon)["settings"]))
        self.assertIn("- Niveau de service : differents (default -> priority). Limite :", text)
        self.assertIn("changements de niveau de service : 0 avant, 0 apres", text)
        text = chr(10).join(CMP.settings_lines(CMP.compare(CMP.measure(self.view, self.cfg), afternoon)["settings"]))
        self.assertIn("niveaux MELANGES dans une periode", text)
        self.assertIn("changements de niveau de service : 1 avant, 0 apres", text)
        no_settings = CMP.measure(slice_view(self.view, parse_when("2026-09-19T10:00:30Z"), parse_when("2026-09-19T10:01:00Z")), self.cfg)
        self.assertFalse(no_settings["settings"]["known"])
        self.assertIn("non releve (aucun reglage de fil ecrit par le client)", CMP.settings_text(no_settings["settings"]))

    def test_reference_keeps_its_settings_and_an_older_one_is_rendered_as_not_recorded(self) -> None:
        ref_path = Path(self.tmp.name) / "ref.json"
        self._cli("compare", "--client", "codex", "--save-reference", str(ref_path), "--since", "2026-09-19T00:00:00Z", "--until", NOON)
        ref = json.loads(ref_path.read_text(encoding="utf-8"))
        self.assertEqual([c["service_tier"] for c in ref["measure"]["settings"]["configs"]], ["default"])
        self.assertIn("- Reglages de fil (modele / effort / niveau de service) : gpt-6-astra / max / default (1 releve(s)", CMP.render_reference(ref))
        out = self._cli("compare", "--client", "codex", "--reference", str(ref_path), "--since", NOON)
        self.assertIn("- Niveau de service : differents (default -> priority)", out)
        del ref["measure"]["settings"]                                           # * reference historique, non reecrite
        old_path = Path(self.tmp.name) / "ref-ancienne.json"
        old_path.write_text(json.dumps(ref), encoding="utf-8")
        self.assertIn("- Reglages de fil (modele / effort / niveau de service) : non releve (mesure anterieure a ce releve)",
                      CMP.render_reference(json.loads(old_path.read_text(encoding="utf-8"))))
        out = self._cli("compare", "--client", "codex", "--reference", str(old_path), "--since", NOON)
        self.assertIn("avant : non releve (mesure anterieure a ce releve) ; apres : gpt-6-astra / max / priority", out)
        self.assertIn("- Niveau de service : non releve. Limite :", out)


if __name__ == "__main__":
    unittest.main()
