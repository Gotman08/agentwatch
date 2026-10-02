"""Observed Codex record shapes, synthetic identifiers and payloads only."""
import json
import tempfile
import unittest
from pathlib import Path

from agentwatch.collector.rollouts import RolloutReader
from agentwatch.collector.store import EventStore
from agentwatch.config import load_config
from agentwatch.core.correlate import build_session
from agentwatch.reports.token_coverage import allocation_coverage
from tests.test_rollouts import CHILD, ROOT, RolloutBuilder


class RolloutCorrelationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cfg = load_config(self.home)
        self.store = EventStore(self.home, self.cfg)

    def reader(self, builder, state=None):
        return RolloutReader(str(self.home / f'rollout-2026-09-11T05-00-00-{builder.thread_id}.jsonl'),
                             self.store, self.cfg, b'synthetic-key', state)

    @staticmethod
    def feed(reader, lines, offset=0):
        for line in lines:
            raw = (line + '\n').encode()
            reader.feed(raw, offset)
            offset += len(raw)
        return offset

    def test_direct_call_identifiers_survive_without_promoting_turn_identity(self):
        b = RolloutBuilder(CHILD, ROOT).meta().turn('context-turn')
        b.fn('call-direct', 'mcp__fixture', 'read', {}, 'SECRET OUTPUT',
             [b.mcp('call-direct', 'fixture', 'read', {}, 'SECRET OUTPUT')])
        b.usage(1000, 800, 50)
        reader = self.reader(b)
        self.feed(reader, b.lines)
        v = build_session(reader.events, self.cfg)
        self.assertEqual(len(v.calls), 1)
        c = v.calls[0]
        self.assertEqual(c.agent_id, CHILD)
        self.assertEqual(c.session_id, ROOT)
        self.assertEqual(c.evidence['source_identity_start']['id'], 'fc_call-direct')
        self.assertEqual(c.evidence['source_identity_end']['item_id'], 'call-direct')
        self.assertEqual(c.evidence['source_identity_end']['turn_id'], 't')
        self.assertEqual(c.evidence['call_identity_basis'], 'matching_function_call_id')
        provenance = c.evidence['allocation_correlation']
        self.assertEqual(provenance['operation_to_emitter'], 'function_call_id')
        self.assertEqual(provenance['emitter']['source_identity']['session_id'], ROOT)
        self.assertEqual(provenance['emitter']['source_identity']['thread_id'], CHILD)
        self.assertEqual(provenance['emitter']['id_origin'], 'source_field')
        self.assertEqual(provenance['emission_rule'], 'pending_calls_at_usage_record')
        self.assertEqual(provenance['output']['source_identity']['call_id'], 'call-direct')
        self.assertEqual(provenance['output']['source_identity']['id'], 'fo_call-direct')
        marker = next(m for m in v.markers if m.meta.get('usage', {}).get('scope') == 'response')
        self.assertEqual(marker.meta['source_identity']['response_id'], provenance['emitter']['response_id'])
        self.assertNotIn('SECRET', json.dumps(reader.events))

    def test_nested_allocation_reports_the_rule_and_both_response_sources(self):
        b = RolloutBuilder(ROOT).meta().turn('turn')
        b.exec_('parent', [b.mcp('nested-1', 'fixture', 'read', {}, 'a'),
                          b.mcp('nested-2', 'fixture', 'read', {}, 'a')], (1000, 800, 40))
        b.usage(1200, 1100, 20)
        reader = self.reader(b)
        self.feed(reader, b.lines)
        v = build_session(reader.events, self.cfg)
        self.assertEqual(sum(c.usage['output_tokens'] for c in v.calls), 40)
        self.assertEqual(sum(c.usage['uncached_input_tokens'] for c in v.calls), 100)
        for c in v.calls:
            p = c.evidence['allocation_correlation']
            self.assertEqual(p['parent_source_identity']['call_id'], 'parent')
            self.assertEqual(p['parent_source_identity']['id'], 'ctc_parent')
            self.assertEqual(p['output']['source_identity']['call_id'], 'parent')
            self.assertEqual(p['output']['source_identity']['id'], 'o_parent')
            self.assertEqual(p['operation_to_emitter'], 'last_open_exec_in_log')
            self.assertLess(p['emitter']['source']['line'], p['consumer']['source']['line'])
        d = allocation_coverage(v, v.calls)
        self.assertEqual(d['reasons'], {'complete_allocation': 2})
        self.assertEqual(d['details'][0]['correlation_provenance']['operation_to_emitter'], 'last_open_exec_in_log')
        self.assertIsNone(d['saving_estimate'])

    def test_item_after_closed_exec_stays_unlinked_despite_nearby_counters(self):
        b = RolloutBuilder(ROOT).meta().turn('turn')
        b.exec_('yielded', [], (1000, 800, 100), 'Script running with cell ID 7')
        b.fn('wait-call', 'functions', 'wait', {'cell_id': '7'}, 'SECRET OUTPUT',
             [b.mcp('late-item', 'fixture', 'read', {}, 'SECRET OUTPUT')])
        b.usage(1300, 1100, 60)
        reader = self.reader(b)
        self.feed(reader, b.lines)
        v = build_session(reader.events, self.cfg)
        c = next(c for c in v.calls if c.call_id == 'late-item')
        self.assertIsNone(c.usage['output_tokens'])
        self.assertIsNone(c.usage['emitter_request_id'])
        self.assertIsNone(c.evidence['allocation_correlation']['emitter'])
        self.assertEqual(c.evidence['call_identity_basis'], 'standalone_item_id')
        self.assertEqual(c.evidence['source_identity_end']['item_id'], 'late-item')
        self.assertEqual(allocation_coverage(v, [c])['reasons'], {'emitter_link_missing_for_standalone_item': 1})

    def test_incremental_resume_preserves_provenance_and_accounting(self):
        b = RolloutBuilder(ROOT).meta().turn('turn')
        b.exec_('parent', [b.mcp('nested', 'fixture', 'read', {}, 'a')], (1000, 800, 40))
        b.usage(1200, 1100, 20)
        full = self.reader(b)
        self.feed(full, b.lines)
        split = next(i + 1 for i, line in enumerate(b.lines) if json.loads(line)['type'] == 'token_usage_record')
        partial = self.reader(b)
        offset = self.feed(partial, b.lines[:split])
        resumed = self.reader(b, json.loads(json.dumps(partial.st)))
        self.feed(resumed, b.lines[split:], offset)
        self.assertEqual(full.events, partial.events + resumed.events)
        # An older importer state has no provenance. It must not invent it.
        old = json.loads(json.dumps(partial.st))
        old.pop('emit_provenance')
        for ex in old['open_execs'].values():
            for key in ('source', 'source_identity', 'emitter_provenance'):
                ex.pop(key)
        legacy = self.reader(b, old)
        self.feed(legacy, b.lines[split:], offset)
        obs = next(e for e in legacy.events if e.get('hook_event_name') == 'rollout_usage')
        self.assertIsNone(obs['evidence']['allocation_correlation']['emitter'])
        self.assertEqual(obs['usage']['output_tokens'], 40)

    def test_missing_response_id_is_labelled_generated_and_payloads_not_copied(self):
        b = RolloutBuilder(ROOT).meta().turn('turn')
        b.fn('direct', None, 'read', {}, 'SECRET OUTPUT')
        b.usage(1200, 1100, 20)
        lines = []
        for line in b.lines:
            record = json.loads(line)
            if record['type'] == 'token_usage_record':
                record['payload'].pop('response_id')
                record['payload']['private_metadata'] = {'content': 'SECRET METADATA'}
            lines.append(json.dumps(record))
        reader = self.reader(b)
        self.feed(reader, lines)
        c = build_session(reader.events, self.cfg).calls[0]
        self.assertEqual(c.evidence['allocation_correlation']['emitter']['id_origin'], 'generated')
        self.assertNotIn('private_metadata', json.dumps(reader.events))
        self.assertNotIn('SECRET', json.dumps(reader.events))
        reader.session_usage_event()
        self.assertNotIn('source_identity', reader.events[-1]['evidence'])


if __name__ == '__main__':
    unittest.main()
