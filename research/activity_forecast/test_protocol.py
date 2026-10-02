"""Research invariants: missingness, split leakage, graph controls and frozen masks."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import torch

from .data import FAMILIES, make_graph, windows
from .experiment import macro_mae, weights_for
from .models import model_for, predict
from .occlusion import perturb, rank_comparison, references


def protocol():
    return {'calls_per_bin': 4, 'history_bins': 8, 'target_bins': 2, 'hidden_units': 4,
            'threads': 1, 'max_epochs': 2, 'patience': 1, 'batch_size': 8,
            'learning_rate': .003, 'weight_decay': .0001, 'seeds': [7],
            'models': ['training_mean', 'recent_rate', 'temporal_gru', 'stgnn_identity', 'stgnn'],
            'occlusion': {'max_per_test_root': 2}, 'bootstrap': {'seed': 4, 'draws': 32}}


def rows(n=40):
    return [{'session': 'root', 'actor': 'a', 'epoch': 0, 'seq': i, 'call_key': f'a|{i}',
             'start_ns': i * 10**9, 'eligible': True, 'family': i % 8} for i in range(n)]


class ResearchTests(unittest.TestCase):
    def test_observed_blocks_count_exactly_and_do_not_invent_silent_windows(self):
        r = rows()
        for row in r[20:]:
            row['start_ns'] += 86400 * 10**9
        samples, _ = windows(r, protocol())
        self.assertEqual(len(samples), 1)
        s = samples[0]
        np.testing.assert_array_equal(s['x'].sum(-1), np.full(8, 4))
        self.assertEqual(s['y'].sum(), 8)
        self.assertEqual(s['wall_clock_collection_coverage'], 'unknown')
        self.assertEqual(len(s['input_calls']), 32)
        self.assertEqual(len(s['target_calls']), 8)
        self.assertFalse(set(s['input_calls']) & set(s['target_calls']))

    def test_invalid_calls_break_sequences_and_equal_time_boundaries_are_rejected(self):
        r = rows(48)
        r[20]['eligible'] = False
        self.assertEqual(windows(r, protocol())[0], [])
        r = rows()
        r[32]['start_ns'] = r[31]['start_ns']
        samples, why = windows(r, protocol())
        self.assertEqual(samples, [])
        self.assertEqual(why['unordered_boundaries'], 1)

    def test_actors_epochs_and_roots_never_join(self):
        for field in ('actor', 'epoch', 'session'):
            r = rows()
            for row in r[20:]:
                row[field] = 1 if field == 'epoch' else 'other'
            self.assertEqual(windows(r, protocol())[0], [])

    def test_session_balancing_and_metric_are_not_dominated_by_largest_root(self):
        sid = np.array(['small', 'large', 'large', 'large'])
        weights = weights_for(sid)
        self.assertAlmostEqual(weights[sid == 'small'].sum(), weights[sid == 'large'].sum())
        pred = np.array([[4.], [0.], [0.], [0.]])
        self.assertEqual(macro_mae(pred, np.zeros_like(pred), sid), 2)

    def test_graph_and_identity_have_same_capacity_and_graph_changes_predictions(self):
        rng = np.random.default_rng(0)
        x = rng.multinomial(4, np.full(8, 1/8), size=(8, 8)).astype(np.float32)
        graph = make_graph(x, np.array(['a'] * 4 + ['b'] * 4))
        np.testing.assert_allclose(graph.sum(1), 1, atol=1e-6)
        torch.manual_seed(4)
        spatial = model_for('stgnn', graph, protocol())
        torch.manual_seed(4)
        identity = model_for('stgnn_identity', graph, protocol())
        self.assertEqual(sum(p.numel() for p in spatial.parameters()), sum(p.numel() for p in identity.parameters()))
        a, b = predict(spatial, x), predict(identity, x)
        self.assertFalse(np.allclose(a, b, atol=1e-8))
        np.testing.assert_allclose(a.sum(-1), 8, atol=1e-5)
        self.assertTrue((a >= 0).all())

    def test_references_are_training_only_and_occlusion_is_an_actual_replacement(self):
        x = np.zeros((2, 8, 8), dtype=np.float32)
        x[0, :, 0] = 4
        x[1, :, 1] = 4
        refs, medoid = references(x, np.array(['a', 'b']), np.array(['first', 'second']))
        self.assertIn(medoid, ('first', 'second'))
        np.testing.assert_allclose(refs['training_mean_equal_root'].sum(-1), 4)
        mask = np.zeros((8, 8), dtype=bool)
        mask[:, 0] = True
        changed = perturb(x, mask, refs['zero'])
        self.assertTrue((changed[:, :, 0] == 0).all())
        np.testing.assert_array_equal(changed[:, :, 1:], x[:, :, 1:])
        self.assertEqual(x[0, 0, 0], 4)
        self.assertEqual(rank_comparison(np.zeros(8), np.zeros(8)), {'spearman': None, 'top3_jaccard_with_ties': None})

    def test_changing_test_labels_cannot_change_training_selection_graph_or_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rng = np.random.default_rng(12)
            x = rng.multinomial(4, np.full(8, 1/8), size=(24, 8)).astype(np.float32)
            y = rng.multinomial(8, np.full(8, 1/8), size=24).astype(np.float32)
            session = np.repeat(['train1', 'train2', 'valid', 'test'], 6)
            split = np.repeat(['train', 'train', 'validation', 'test'], 6)
            p = protocol()
            p['session_splits'] = dict(zip(session, split))
            (root / 'protocol.json').write_text(json.dumps(p))
            manifests = []
            for run in range(2):
                labels = y.copy()
                if run:
                    labels[split == 'test'] = np.roll(labels[split == 'test'], 2, axis=1)
                data = root / f'data{run}.npz'
                np.savez(data, x=x, y=labels, session=session, split=split, sample_id=np.array([f's{i:03}' for i in range(24)]))
                out = root / f'run{run}'
                result = subprocess.run([sys.executable, '-m', 'research.activity_forecast.experiment', '--dataset', str(data),
                                         '--protocol', str(root / 'protocol.json'), '--out', str(out)],
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifests.append(json.loads((out / 'freeze.json').read_text()))
            for key in ('checkpoints', 'graph_sha256', 'references_sha256', 'selected_stgnn_seed'):
                self.assertEqual(manifests[0][key], manifests[1][key], key)


if __name__ == '__main__':
    unittest.main()
