# AgentWatch — observer et comprendre le travail des agents

## Recueillir les appels des agents {#collecter}
tech: agentwatch/hook_entry.py, agentwatch/collector/ingest.py, agentwatch/adapters/
files: [agentwatch/hook_entry.py, agentwatch/adapters/**, agentwatch/collector/ingest.py, agentwatch/collector/privacy.py, agentwatch/collector/store.py, agentwatch/collector/rollouts.py, agentwatch/collector/transcripts.py, agentwatch/collector/follow.py]
links: [relier, installer]
- [x] Conserver les observations locales Codex et Claude {#collecte-locale}
  tech: agentwatch/collector/store.py, agentwatch/collector/rollouts.py, agentwatch/collector/transcripts.py
- [x] Masquer les donnees sensibles avant leur stockage {#masquer}
  tech: agentwatch/collector/privacy.py, tests/test_privacy_normalize.py

## Relier les observations aux appels {#relier}
tech: agentwatch/core/schema.py, agentwatch/core/correlate.py, agentwatch/core/intent.py
files: [agentwatch/core/**]
needs: [collecter]
links: [detecter, expliquer]
- [x] Distinguer les appels, sessions et sous-agents {#correler}
  tech: agentwatch/core/correlate.py, tests/test_agents.py
- [x] Conserver les resultats inconnus comme inconnus {#statuts}
  tech: agentwatch/core/schema.py, tests/test_exit_status_and_failures.py

## Reperer le travail repetitif {#detecter}
tech: agentwatch/detectors/
files: [agentwatch/detectors/**]
needs: [relier]
links: [expliquer]
- [x] Signaler repetitions, erreurs et operations regroupables {#regles}
  tech: agentwatch/detectors/repeated_calls.py, agentwatch/detectors/error_loops.py, agentwatch/detectors/batchable.py
- [x] Reperer les consignes repetees et automatisations possibles {#automatisations}
  tech: agentwatch/detectors/repeated_guidance.py, agentwatch/detectors/automation_candidates.py, agentwatch/detectors/tool_gap.py

## Expliquer les observations et leurs limites {#expliquer}
tech: agentwatch/reports/, agentwatch/reports/replacements.py
files: [agentwatch/reports/**]
needs: [relier, detecter]
links: [afficher]
- [x] Produire les rapports et tendances multi-sessions {#rapports}
  tech: agentwatch/reports/markdown.py, agentwatch/reports/json_report.py, agentwatch/reports/trends.py
- [x] Evaluer les remplacements avec des preuves trivalues {#remplacements}
  tech: agentwatch/reports/observed_graph.py, agentwatch/reports/replacements.py, tests/test_replacements.py

## Installer et surveiller la collecte {#installer}
tech: agentwatch/installer/, agentwatch/collector/health.py
files: [agentwatch/installer/**, agentwatch/collector/health.py, examples/config/**]
links: [collecter, verifier]
- [x] Preparer les hooks avec sauvegardes et retrait {#hooks}
  tech: agentwatch/installer/claude_code.py, agentwatch/installer/codex.py, agentwatch/installer/common.py
- [x] Diagnostiquer la sante de la collecte {#sante}
  tech: agentwatch/collector/health.py, tests/test_health.py

## Afficher le travail dans le tableau de projet {#afficher}
tech: agentwatch/cli.py, agentwatch/connectors/agenttrail.py, examples/agenttrail_map.mjs
files: [agentwatch/cli.py, agentwatch/connectors/**, examples/agenttrail_map.mjs, docs/agenttrail.md, README.md, PLAN.md]
needs: [collecter]
links: [expliquer, verifier]
- [x] Relayer les sessions et outils vers AgentTrail Map {#connecter-map}
  by: codex
  from: agent
  tech: agentwatch/connectors/agenttrail.py, examples/agenttrail_map.mjs
- [x] Verifier le relais avec le vrai serveur Map {#valider-map}
  by: codex
  from: agent
  tech: tests/test_agenttrail.py, tests/test_agenttrail_integration.py

## Verifier et documenter les comportements {#verifier}
tech: tests/, docs/, agentwatch/selftest.py
files: [tests/**, docs/architecture.md, docs/events.md, docs/compatibility.md, docs/detectors.md, docs/privacy.md, docs/installation.md, docs/run-inspection.md, docs/replacements.md, docs/claude-context.md, agentwatch/selftest.py, examples/replacement_replay.py, examples/development_usage.py, examples/claude_spec_replay.py, examples/claude_run_replay.py, examples/demo/**, examples/reports/**]
links: [collecter, relier, detecter, expliquer, installer, afficher]
- [x] Couvrir les contrats par des scenarios reproductibles {#contrats}
  tech: tests/test_adapters_contract.py, tests/test_store_concurrency.py, agentwatch/selftest.py

## decisions
- 2026-10-11 : carte reconstruite depuis les modules presents. Les coches indiquent une implementation identifiable dans les fichiers cites ; elles ne prouvent ni acceptation utilisateur ni gain mesure.
- 2026-10-11 : connecter AgentWatch au tableau AgentTrail Map, a la demande du proprietaire. Les appels d'outils ne doivent pas etre transformes en taches terminees, ni les lectures en modifications de fichiers. La collecte existante et ses journaux sont conserves.
