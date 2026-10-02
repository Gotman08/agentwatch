"""Tests of masking, donor provenance, missing information and replay."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from .study import (comparisons, context_donors, evaluate, load_inputs, make_plan,
                    rank_compare, replace_block, static_donors, summarize, valid_counts)


def protocol():
    return {'calls_per_bin': 4, 'history_bins': 8, 'families': 8,
            'block_starts': [0, 2, 4, 6], 'block_width': 2, 'tie_tolerance': 1e-8}


def sample_data():
    rng = np.random.default_rng(31)
    x = rng.multinomial(4, np.full(8, 1/8), size=(9, 8)).astype(np.float32)
    return x, np.array(['a']*4 + ['b']*3 + ['test']*2), np.array([f'{i:02}' for i in range(9)])


def simple_predictor(x):
    return (x * np.arange(1, 9)[None, :, None]).sum(1) / 36


class BlockStudyTests(unittest.TestCase):
    def test_block_replacement_preserves_integer_totals_visible_values_and_input(self):
        x, _, _ = sample_data()
        before = x.copy()
        for start in (0, 2, 4, 6):
            changed = replace_block(x[:2], x[2:4], start, 2)
            self.assertTrue(valid_counts(changed))
            np.testing.assert_array_equal(changed[:, start:start+2], x[2:4, start:start+2])
            np.testing.assert_array_equal(changed[:, :start], x[:2, :start])
            np.testing.assert_array_equal(changed[:, start+2:], x[:2, start+2:])
        np.testing.assert_array_equal(x, before)
        bad = x.copy(); bad[0, 0, 0] += .5
        self.assertFalse(valid_counts(bad))
        bad = x.copy(); bad[0, 0] = 0
        self.assertFalse(valid_counts(bad))
        bad = x.copy(); bad[0, 0, 0] = -1
        self.assertFalse(valid_counts(bad))

    def test_context_selector_is_blind_to_masked_query_and_candidate_values(self):
        x, roots, ids = sample_data()
        for start in (0, 2, 4, 6):
            original = context_donors(x[-1], x[:7], roots[:7], ids[:7], start, 2)
            changed_query = x[-1].copy()
            changed_train = x[:7].copy()
            changed_query[start:start+2] = np.nan
            changed_train[:, start:start+2] = np.nan
            blind = context_donors(changed_query, changed_train, roots[:7], ids[:7], start, 2)
            np.testing.assert_array_equal(original, blind)

    def test_context_selector_uses_visible_context_and_breaks_ties_by_id(self):
        train = np.zeros((3, 8, 8), dtype=np.float32)
        train[0, :, 0] = 4
        train[1, :, 1] = 4
        train[2] = train[1]
        roots, ids = np.array(['a']*3), np.array(['z', 'b', 'a'])
        self.assertEqual(context_donors(train[0], train, roots, ids, 0, 2)[0], 0)
        self.assertEqual(context_donors(train[1], train, roots, ids, 0, 2)[0], 2)

    def test_plans_use_only_training_donors_and_one_reference_per_root(self):
        x, roots, ids = sample_data()
        policies, plan = make_plan(x[7:], x[:7], roots[:7], ids[:7], '00', protocol())
        self.assertEqual(plan.shape, (2, 5, 4))
        self.assertEqual(len(policies), 5)
        self.assertTrue((plan < 7).all())
        self.assertTrue((plan[:, 0] == 0).all())
        np.testing.assert_array_equal(static_donors(roots[:7], ids[:7]), [0, 4])
        self.assertTrue((plan[:, 1] == 0).all())
        self.assertTrue((plan[:, 2] == 4).all())
        for i, p in enumerate(policies):
            self.assertTrue((roots[plan[:, i]] == p['root']).all())
        with self.assertRaises(ValueError):
            make_plan(x[7:], x[:7], roots[:7], ids[:7], '07', protocol())

    def test_loader_never_opens_future_targets(self):
        x, roots, ids = sample_data()
        allowed = {'x': x, 'split': np.array(['train']*7+['test']*2), 'session': roots, 'sample_id': ids}
        opened = []
        class Archive:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def __getitem__(self, key):
                opened.append(key)
                if key not in allowed:
                    raise AssertionError('Forbidden future target access')
                return allowed[key]
        with patch('research.block_occlusion.study.np.load', return_value=Archive()):
            actual = load_inputs('unused')
        self.assertEqual(set(opened), set(allowed))
        for key in allowed:
            np.testing.assert_array_equal(actual[key], allowed[key])

    def test_no_op_and_actual_prediction_changes_are_distinct(self):
        x, roots, ids = sample_data()
        query = np.stack([x[0], x[7]])
        policies, plan = make_plan(query, x[:7], roots[:7], ids[:7], '00', protocol())
        original, importance, rows = evaluate(simple_predictor, query, x[:7], plan, policies,
                                               np.array(['q0', 'q1']), np.array(['test']*2), ids[:7], protocol())
        same = [r for r in rows if r['sample_id'] == 'q0' and r['policy'] == 'fixed']
        self.assertEqual(len(same), 4)
        self.assertTrue(all(r['no_op'] and r['importance'] == 0 for r in same))
        self.assertTrue(any(not r['no_op'] and r['importance'] > 0 for r in rows))
        self.assertTrue(all(r['bin_totals_preserved'] and r['nonnegative_integer_counts'] for r in rows))

    def test_ranking_ties_and_constant_vectors_do_not_invent_stability(self):
        self.assertIsNone(rank_compare([1, 1, 1, 1], [4, 3, 2, 1])['spearman'])
        self.assertIsNone(rank_compare([0, 0, 0, 0], [0, 0, 0, 0])['same_top1_set'])
        a = rank_compare([4, 3, 2, 1], [1, 2, 3, 4])
        self.assertEqual(a['spearman'], -1)
        self.assertEqual(a['top1_jaccard'], 0)
        self.assertFalse(a['same_top1_set'])
        b = rank_compare([4, 4, 2, 1], [4, 3, 2, 1])
        self.assertEqual(b['top1_jaccard'], .5)
        self.assertFalse(b['same_top1_set'])

    def test_future_label_changes_leave_entire_explanation_pipeline_identical(self):
        x, roots, ids = sample_data()
        result = []
        with tempfile.TemporaryDirectory() as tmp:
            for version in range(2):
                path = Path(tmp) / f'{version}.npz'
                labels = np.zeros((9, 8), dtype=np.float32)
                labels[:, version] = 8
                np.savez(path, x=x, y=labels, session=roots, sample_id=ids,
                         split=np.array(['train']*7+['test']*2))
                data = load_inputs(path)
                tr, te = data['split'] == 'train', data['split'] == 'test'
                p, plan = make_plan(data['x'][te], data['x'][tr], data['session'][tr], data['sample_id'][tr], '00', protocol())
                original, scores, rows = evaluate(simple_predictor, data['x'][te], data['x'][tr], plan, p,
                    data['sample_id'][te], data['session'][te], data['sample_id'][tr], protocol())
                ranks, vectors = comparisons(scores, p, data['sample_id'][te], data['session'][te], protocol())
                summary = summarize(ranks, rows, scores, p, data['session'][te], protocol())
                result.append(json.dumps({'plan': plan.tolist(), 'predictions': original.tolist(),
                                           'rows': rows, 'ranks': ranks, 'summary': summary}, sort_keys=True))
        self.assertEqual(result[0], result[1])


if __name__ == '__main__':
    unittest.main()
