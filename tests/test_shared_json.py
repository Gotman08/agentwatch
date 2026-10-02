import copy
import json
import unittest

from agentwatch.reports.json_report import build_report
from agentwatch.reports.markdown import render_markdown
from agentwatch.reports.stats import compute_stats, coverage_matrix
from agentwatch.reports.shared_json import compact_report, expand_report, REF
from tests.test_replacements import call, finding, view


class SharedJSONTests(unittest.TestCase):
    def test_real_report_values_references_and_rendering_are_preserved(self):
        calls = [call(i) for i in range(8)]
        v = view(calls)
        f = finding(calls)
        stats = compute_stats(v, {})
        coverage = coverage_matrix(v)
        report = build_report(v, stats, coverage, [f], {}, {}, None)
        before = copy.deepcopy(report)
        compact = compact_report(report)
        expanded = expand_report(json.loads(json.dumps(compact)))
        self.assertEqual(expanded, report)
        self.assertEqual(report, before)
        self.assertEqual(render_markdown(expanded), render_markdown(report))
        self.assertEqual(compact['report_version'], '2.0')
        self.assertEqual(report['report_version'], '1.1')
        self.assertLess(len(json.dumps(compact)), len(json.dumps(report)))
        # La voie compacte peut eviter la copie profonde sans modifier les resultats.
        borrowed = build_report(v, stats, coverage, [f], {}, {}, None, copy_findings=False)
        self.assertEqual(expand_report(compact_report(borrowed)), report)

    def test_literal_reference_shaped_user_values_are_not_interpreted(self):
        data = {'report_version':'1.1','params': [{REF: 'anything'}, {REF: {REF: 'nested'}}],
                'literal': {REF: 'not-a-reference', 'x': 1}}
        self.assertEqual(expand_report(compact_report(data)), data)

    def test_unresolved_cycles_and_unknown_versions_fail_explicitly(self):
        base = {'report_version':'2.0', 'encoding':'agentwatch-shared-json-v1',
                'expanded_report_version':'1.1', 'report':{REF:'a'}, 'shared':{}}
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            expand_report(base)
        base['shared'] = {'a': {'child':{REF:'a'}}}
        with self.assertRaisesRegex(ValueError, 'cyclic'):
            expand_report(base)
        base['report_version']='3.0'
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            expand_report(base)

    def test_expansion_does_not_alias_repeated_objects(self):
        item={'x':'long'*300}
        data={'report_version':'1.1','a':item,'b':copy.deepcopy(item)}
        expanded=expand_report(compact_report(data))
        expanded['a']['x']='changed'
        self.assertEqual(expanded['b'],item)
