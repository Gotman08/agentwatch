"""Deterministic CPU experiment. Freeze by validation before opening test metrics."""
from __future__ import annotations

import argparse
import copy
import csv
import json
from pathlib import Path
import time

import numpy as np
import torch

from .data import FAMILIES, make_graph, save_json, sha
from .models import model_for, predict
from .occlusion import explain, references


def session_scores(pred, target, sessions):
    rows = []
    for sid in np.unique(sessions):
        diff = pred[sessions == sid] - target[sessions == sid]
        rows.append({'session': str(sid), 'windows': len(diff), 'mae': float(np.abs(diff).mean()),
                     'rmse': float(np.sqrt((diff ** 2).mean())), 'mae_by_family': np.abs(diff).mean(0).tolist()})
    return rows


def macro_mae(pred, target, sessions):
    return float(np.mean([r['mae'] for r in session_scores(pred, target, sessions)]))


def weights_for(sessions):
    counts = {sid: int((sessions == sid).sum()) for sid in np.unique(sessions)}
    return np.array([len(sessions) / (len(counts) * counts[sid]) for sid in sessions], dtype=np.float32)


def fit(name, seed, graph, protocol, train, valid):
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    model = model_for(name, graph, protocol)
    optimizer = torch.optim.AdamW(model.parameters(), lr=protocol['learning_rate'], weight_decay=protocol['weight_decay'])
    x, y, sessions = train
    vx, vy, vs = valid
    xt, yt = torch.from_numpy(x), torch.from_numpy(y)
    weights = torch.from_numpy(weights_for(sessions))
    history, best, best_state, best_epoch = [], float('inf'), None, 0
    started = time.perf_counter()
    for epoch in range(1, protocol['max_epochs'] + 1):
        model.train()
        order = rng.permutation(len(x))
        loss_sum = 0.0
        for start in range(0, len(order), protocol['batch_size']):
            ix = order[start:start+protocol['batch_size']]
            optimizer.zero_grad(set_to_none=True)
            per_example = ((model(xt[ix]) - yt[ix]) ** 2).mean(-1)
            loss = (per_example * weights[ix]).mean()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            loss_sum += float(loss.detach()) * len(ix)
        validation = macro_mae(predict(model, vx), vy, vs)
        history.append({'epoch': epoch, 'train_mse_weighted': loss_sum / len(x), 'validation_macro_mae': validation})
        if validation < best - 1e-5:
            best, best_epoch, best_state = validation, epoch, copy.deepcopy(model.state_dict())
        if epoch - best_epoch >= protocol['patience']:
            break
    model.load_state_dict(best_state)
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model, {'model': name, 'seed': seed, 'best_epoch': best_epoch, 'epochs_run': epoch,
                   'validation_macro_mae': best, 'parameters': sum(p.numel() for p in model.parameters()),
                   'training_seconds': time.perf_counter() - started, 'history': history}


def bootstrap_comparison(left, right, protocol):
    """Paired root-level resampling of seed-averaged MAEs; no IID-window CI."""
    delta = np.asarray(right) - np.asarray(left)  # positive means left improves
    rng = np.random.default_rng(protocol['bootstrap']['seed'])
    draws = rng.choice(len(delta), (protocol['bootstrap']['draws'], len(delta)), replace=True)
    interval = np.quantile(delta[draws].mean(-1), [0.025, 0.975])
    return {'root_count': len(delta), 'mae_reduction': float(delta.mean()),
            'relative_reduction_percent': float(100 * delta.mean() / np.mean(right)) if np.mean(right) else None,
            'exploratory_root_bootstrap_interval_95': interval.tolist(),
            'roots_improved': int((delta > 0).sum()), 'root_differences': delta.tolist()}


def run(dataset, protocol_path, out):
    protocol = json.loads(Path(protocol_path).read_text(encoding='utf-8'))
    data = np.load(dataset, allow_pickle=False)
    x, y, split, sessions, ids = (data[k] for k in ('x', 'y', 'split', 'session', 'sample_id'))
    assert np.isfinite(x).all() and np.isfinite(y).all() and (x >= 0).all() and (y >= 0).all()
    assert np.allclose(x.sum(-1), protocol['calls_per_bin'])
    assert np.allclose(y.sum(-1), protocol['calls_per_bin'] * protocol['target_bins'])
    grouped = {s: np.flatnonzero(split == s) for s in ('train', 'validation', 'test')}
    root_sets = {s: set(sessions[ix]) for s, ix in grouped.items()}
    assert not (root_sets['train'] & root_sets['validation'] or root_sets['train'] & root_sets['test']
                or root_sets['validation'] & root_sets['test'])
    assert all(protocol['session_splits'][str(sid)] == str(sp) for sid, sp in zip(sessions, split))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    models_dir = out / 'checkpoints'
    models_dir.mkdir(exist_ok=True)
    assert not (out / 'freeze.json').exists(), 'Use a fresh output directory; do not overwrite a frozen experiment.'
    torch.set_num_threads(protocol['threads'])
    torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    tr, va, te = (grouped[s] for s in ('train', 'validation', 'test'))
    graph = make_graph(x[tr], sessions[tr])
    baseline_arrays, medoid_id = references(x[tr], sessions[tr], ids[tr])
    save_json(out / 'graph.json', {'families': FAMILIES, 'adjacency': graph.tolist(), 'source': 'training_inputs_only',
                                 'edge_semantics': 'lagged_association_not_dependency', 'root_sessions': sorted(root_sets['train'])})
    save_json(out / 'occlusion-references.json', {'arrays': {k: v.tolist() for k, v in baseline_arrays.items()},
                                               'medoid_sample_id': medoid_id, 'source': 'training_inputs_only'})
    prior = np.mean([y[tr][sessions[tr] == sid].mean(0) for sid in sorted(root_sets['train'])], axis=0)
    trained, history = {}, []
    for name in ('temporal_gru', 'stgnn_identity', 'stgnn'):
        for seed in protocol['seeds']:
            model, result = fit(name, seed, graph, protocol, (x[tr], y[tr], sessions[tr]), (x[va], y[va], sessions[va]))
            filename = f'{name}-{seed}.pt'
            torch.save(model.state_dict(), models_dir / filename)
            result['checkpoint'] = filename
            result['checkpoint_sha256'] = sha(models_dir / filename)
            trained[(name, seed)] = model
            history.append(result)
            print(json.dumps({k: v for k, v in result.items() if k != 'history'}), flush=True)
    best = min((r for r in history if r['model'] == 'stgnn'), key=lambda r: (r['validation_macro_mae'], r['seed']))
    # This artifact is written BEFORE any held-out scoring or explanation.
    frozen = {'protocol_sha256': sha(protocol_path), 'dataset_sha256': sha(dataset),
              'graph_sha256': sha(out / 'graph.json'), 'references_sha256': sha(out / 'occlusion-references.json'),
              'selected_stgnn_seed': best['seed'], 'selection_uses': 'validation_macro_mae_only',
              'checkpoints': [{k: r[k] for k in ('model', 'seed', 'checkpoint', 'checkpoint_sha256', 'best_epoch')} for r in history],
              'stage': 'all_weights_and_references_frozen_before_test_scoring', 'test_roots': sorted(root_sets['test']),
              'versions': {'torch': torch.__version__, 'numpy': np.__version__}}
    save_json(out / 'training.json', history)
    save_json(out / 'freeze.json', frozen)

    predictions = {('training_mean', 0): np.broadcast_to(prior, y[te].shape).copy(),
                   ('recent_rate', 0): x[te, -protocol['target_bins']:].sum(1)}
    for key, model in trained.items():
        predictions[key] = predict(model, x[te])
    test_scores, prediction_arrays = [], {}
    for (name, seed), pred in predictions.items():
        rows = session_scores(pred, y[te], sessions[te])
        test_scores.append({'model': name, 'seed': seed, 'macro_mae': float(np.mean([r['mae'] for r in rows])),
                            'macro_rmse': float(np.mean([r['rmse'] for r in rows])), 'sessions': rows})
        prediction_arrays[f'{name}-{seed}'] = pred
    np.savez_compressed(out / 'test-predictions.npz', target=y[te], sample_id=ids[te], session=sessions[te], **prediction_arrays)
    save_json(out / 'test-metrics.json', test_scores)
    root_order = sorted(root_sets['test'])
    by_model = {}
    for name in protocol['models']:
        rows = [r for r in test_scores if r['model'] == name]
        by_model[name] = np.mean([[next(s['mae'] for s in r['sessions'] if s['session'] == root) for root in root_order] for r in rows], axis=0)
    comparison = {name: bootstrap_comparison(by_model['stgnn'], by_model[name], protocol)
                  for name in ('recent_rate', 'temporal_gru', 'stgnn_identity')}
    save_json(out / 'comparisons.json', {'root_order': root_order, 'stgnn_vs': comparison,
                                      'macro_mae_by_model': {k: float(v.mean()) for k, v in by_model.items()}})
    explanations = explain(trained[('stgnn', best['seed'])], x[te], y[te], sessions[te], ids[te], baseline_arrays, protocol)
    save_json(out / 'occlusion.json', explanations)
    for r in history:
        assert sha(models_dir / r['checkpoint']) == r['checkpoint_sha256']
    assert sha(out / 'graph.json') == frozen['graph_sha256']
    assert sha(out / 'occlusion-references.json') == frozen['references_sha256']
    save_json(out / 'completed.json', {'frozen_weights_graph_references_unchanged': True,
              'split_window_counts': {s: len(ix) for s, ix in grouped.items()},
              'split_root_counts': {s: len(roots) for s, roots in root_sets.items()},
              'occluded_test_examples': len(explanations['originals']), 'selected_stgnn_seed': best['seed'],
              'test_metrics_sha256': sha(out / 'test-metrics.json'), 'occlusion_sha256': sha(out / 'occlusion.json')})
    print(json.dumps({'stage': 'complete', 'comparison': comparison, 'occlusion_examples': len(explanations['originals'])}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    run(args.dataset, args.protocol, args.out)
