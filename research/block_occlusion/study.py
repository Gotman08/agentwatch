"""Frozen-model block occlusions. Donors never depend on targets or masked values."""
from __future__ import annotations

import argparse
from collections import defaultdict
from itertools import combinations
import json
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr
import torch

from research.activity_forecast.data import save_json, sha
from research.activity_forecast.models import model_for, predict


def load_inputs(path):
    """Explicit field allowlist: no target array is opened."""
    with np.load(path, allow_pickle=False) as data:
        return {key: data[key].copy() for key in ('x', 'split', 'session', 'sample_id')}


def valid_counts(x, total=4):
    return bool(np.isfinite(x).all() and (x >= 0).all()
                and np.array_equal(x, np.round(x)) and (x.sum(-1) == total).all())


def visible_bins(steps, start, width):
    if start < 0 or width < 1 or start + width > steps:
        raise ValueError('Invalid block')
    visible = np.ones(steps, dtype=bool)
    visible[start:start + width] = False
    if not visible.any():
        raise ValueError('Context selection requires a visible bin')
    return visible


def static_donors(sessions, ids):
    """One fixed donor per training root; no query or predicted value enters."""
    return np.array([min(np.flatnonzero(sessions == root), key=lambda i: str(ids[i]))
                     for root in np.unique(sessions)], dtype=int)


def context_donors(query, x_train, sessions_train, ids_train, start, width):
    """Ignore the masked part of BOTH query and donor for candidate selection."""
    visible = visible_bins(query.shape[0], start, width)
    distance = ((x_train[:, visible] - query[None, visible]) ** 2).mean(axis=(1, 2))
    if not np.isfinite(distance).all():
        raise ValueError('Visible context must be finite')
    selected = []
    for root in np.unique(sessions_train):
        ix = np.flatnonzero(sessions_train == root)
        order = np.lexsort((ids_train[ix], distance[ix]))
        selected.append(ix[order[0]])
    return np.array(selected, dtype=int)


def replace_block(x, donor, start, width):
    result = np.array(x, copy=True)
    result[..., start:start + width, :] = donor[..., start:start + width, :]
    return result


def rank_compare(a, b, tolerance=1e-8):
    a, b = np.asarray(a), np.asarray(b)
    if np.ptp(a) <= tolerance or np.ptp(b) <= tolerance:
        return {'spearman': None, 'top1_jaccard': None, 'same_top1_set': None}
    sa, sb = set(np.flatnonzero(a >= a.max() - tolerance)), set(np.flatnonzero(b >= b.max() - tolerance))
    return {'spearman': float(spearmanr(a, b).statistic),
            'top1_jaccard': len(sa & sb) / len(sa | sb), 'same_top1_set': sa == sb}


def make_plan(query, x_train, train_sessions, train_ids, fixed_id, protocol):
    if len(set(train_ids)) != len(train_ids):
        raise ValueError('Training sample IDs must be unique')
    roots = np.unique(train_sessions)
    fixed = np.flatnonzero(train_ids == fixed_id)
    if len(fixed) != 1:
        raise ValueError('Fixed reference is not a unique training window')
    fixed_each_root = static_donors(train_sessions, train_ids)
    policies = [{'id': 'fixed', 'kind': 'fixed', 'root': str(train_sessions[fixed[0]])}]
    policies += [{'id': f'historical_{i+1}', 'kind': 'historical', 'root': str(root)} for i, root in enumerate(roots)]
    policies += [{'id': f'context_{i+1}', 'kind': 'context', 'root': str(root)} for i, root in enumerate(roots)]
    plan = np.zeros((len(query), len(policies), len(protocol['block_starts'])), dtype=int)
    plan[:, 0, :] = fixed[0]
    plan[:, 1:1+len(roots), :] = fixed_each_root[None, :, None]
    for row, q in enumerate(query):
        for mask, start in enumerate(protocol['block_starts']):
            plan[row, 1+len(roots):, mask] = context_donors(
                q, x_train, train_sessions, train_ids, start, protocol['block_width'])
    return policies, plan


def evaluate(predictor, query, train, plan, policies, query_ids, query_sessions, train_ids, protocol):
    original = predictor(query)
    shape = plan.shape
    importance = np.zeros(shape, dtype=np.float64)
    rows = []
    for policy, details in enumerate(policies):
        for mask, start in enumerate(protocol['block_starts']):
            selected = plan[:, policy, mask]
            donors = train[selected]
            altered = replace_block(query, donors, start, protocol['block_width'])
            if not valid_counts(altered, protocol['calls_per_bin']):
                raise ValueError('A perturbation broke the count constraints')
            visible = visible_bins(query.shape[1], start, protocol['block_width'])
            if not np.array_equal(altered[:, visible], query[:, visible]):
                raise ValueError('A visible bin changed')
            delta = predictor(altered) - original
            scores = np.abs(delta).mean(-1)
            importance[:, policy, mask] = scores
            for i in range(len(query)):
                noop = np.array_equal(query[i], altered[i])
                if noop and not np.array_equal(delta[i], np.zeros_like(delta[i])):
                    raise ValueError('Non-deterministic prediction for an unchanged input')
                rows.append({'sample_id': str(query_ids[i]), 'session': str(query_sessions[i]),
                             'policy': details['id'], 'reference_kind': details['kind'], 'donor_root': details['root'],
                             'block_start': start, 'block_width': protocol['block_width'],
                             'donor_sample_id': str(train_ids[selected[i]]),
                             'donor_block': altered[i, start:start+protocol['block_width']].tolist(),
                             'importance': float(scores[i]), 'prediction_delta': delta[i].tolist(),
                             'no_op': noop, 'changed_input_cells': int(np.count_nonzero(altered[i] != query[i])),
                             'input_l1_distance': float(np.abs(altered[i] - query[i]).sum()),
                             'visible_context_mse': float(((donors[i, visible] - query[i, visible])**2).mean()),
                             'nonnegative_integer_counts': True, 'bin_totals_preserved': True, 'visible_bins_unchanged': True})
    return original, importance, rows


def comparisons(importance, policies, ids, sessions, protocol):
    keys = [p['id'] for p in policies]
    historical = [i for i, p in enumerate(policies) if p['kind'] == 'historical']
    context = [i for i, p in enumerate(policies) if p['kind'] == 'context']
    vectors = {key: importance[:, i] for i, key in enumerate(keys)}
    vectors['historical_mean'] = importance[:, historical].mean(1)
    vectors['context_mean'] = importance[:, context].mean(1)
    pairs = []
    for group, subset in [('within_historical', historical), ('within_context', context)]:
        pairs += [(group, keys[a], keys[b]) for a, b in combinations(subset, 2)]
    pairs += [('fixed_vs_historical', 'fixed', keys[i]) for i in historical]
    pairs += [('fixed_vs_context', 'fixed', keys[i]) for i in context]
    pairs += [('historical_vs_context_same_root', keys[a], keys[b]) for a, b in zip(historical, context)]
    pairs += [('fixed_vs_historical_mean', 'fixed', 'historical_mean'),
              ('fixed_vs_context_mean', 'fixed', 'context_mean'),
              ('historical_mean_vs_context_mean', 'historical_mean', 'context_mean')]
    rows = []
    for group, left, right in pairs:
        for i, sid in enumerate(ids):
            rows.append({'group': group, 'left': left, 'right': right, 'sample_id': str(sid), 'session': str(sessions[i]),
                         **rank_compare(vectors[left][i], vectors[right][i], protocol['tie_tolerance'])})
    return rows, vectors


def summarize(rankings, perturbations, importance, policies, sessions, protocol):
    summary = []
    for group in dict.fromkeys(r['group'] for r in rankings):
        rows = [r for r in rankings if r['group'] == group]
        per_root = []
        for root in np.unique(sessions):
            selected = [r for r in rows if r['session'] == root]
            defined = [r for r in selected if r['spearman'] is not None]
            per_root.append({'session': str(root), 'pairs': len(selected), 'defined_pairs': len(defined),
                             'spearman_mean': float(np.mean([r['spearman'] for r in defined])) if defined else None,
                             'top1_jaccard_mean': float(np.mean([r['top1_jaccard'] for r in defined])) if defined else None,
                             'top1_set_change_rate': float(np.mean([not r['same_top1_set'] for r in defined])) if defined else None})
        valid = [r for r in per_root if r['defined_pairs']]
        summary.append({'group': group, 'pairs': len(rows), 'defined_pairs': sum(r['defined_pairs'] for r in per_root),
                        'roots_with_defined_pairs': len(valid),
                        'macro_root_spearman': float(np.mean([r['spearman_mean'] for r in valid])) if valid else None,
                        'macro_root_top1_jaccard': float(np.mean([r['top1_jaccard_mean'] for r in valid])) if valid else None,
                        'macro_root_top1_set_change_rate': float(np.mean([r['top1_set_change_rate'] for r in valid])) if valid else None,
                        'per_root': per_root})
    validity = []
    for kind in ('fixed', 'historical', 'context'):
        rows = [r for r in perturbations if r['reference_kind'] == kind]
        per_root = []
        for root in np.unique(sessions):
            selected = [r for r in rows if r['session'] == root]
            per_root.append({'session': str(root), 'perturbations': len(selected),
                             'no_op': sum(r['no_op'] for r in selected),
                             'no_op_rate': float(np.mean([r['no_op'] for r in selected])),
                             'importance_mean': float(np.mean([r['importance'] for r in selected])),
                             'input_l1_mean': float(np.mean([r['input_l1_distance'] for r in selected])),
                             'visible_context_mse_mean': float(np.mean([r['visible_context_mse'] for r in selected]))})
        validity.append({'kind': kind, 'perturbations': len(rows),
                         'invalid_integer_counts': sum(not r['nonnegative_integer_counts'] for r in rows),
                         'invalid_totals': sum(not r['bin_totals_preserved'] for r in rows),
                         'changed_visible_bins': sum(not r['visible_bins_unchanged'] for r in rows),
                         'no_op': sum(r['no_op'] for r in rows),
                         'changed_input_zero_prediction_change': sum(not r['no_op'] and r['importance'] == 0 for r in rows),
                         'macro_root_no_op_rate': float(np.mean([r['no_op_rate'] for r in per_root])),
                         'per_root': per_root})
    return {'ranking_comparisons': summary, 'validity': validity,
            'examples': len(sessions), 'roots': len(np.unique(sessions)),
            'policies': len(policies), 'perturbations': len(perturbations),
            'independent_test': False, 'interpretation': 'Structural constraints verified, joint history plausibility unknown.'}


def check_original(original, protocol):
    if sha(original / 'artifact-manifest.json') != protocol['original_manifest_sha256']:
        raise ValueError('Original manifest changed')
    manifest = json.loads((original / 'artifact-manifest.json').read_text(encoding='utf-8'))
    for record in manifest['files']:
        if sha(original / record['path']) != record['sha256']:
            raise ValueError(f"Original artifact changed: {record['path']}")
    if sha(original / 'experiment/freeze.json') != protocol['original_freeze_sha256']:
        raise ValueError('Original freeze changed')
    if sha(original / 'dataset/dataset.npz') != protocol['dataset_sha256']:
        raise ValueError('Original dataset changed')
    # Verify the actual imported model source, not just a detached copy.
    from research import activity_forecast
    identity = json.loads((original / 'prototype-identity.json').read_text(encoding='utf-8'))
    source_root = Path(activity_forecast.__file__).parent
    for record in identity['files']:
        if sha(source_root / record['path']) != record['sha256']:
            raise ValueError(f"Base research source changed: {record['path']}")
    return len(manifest['files'])


def run(original, protocol_path, out):
    original, protocol_path, out = Path(original), Path(protocol_path), Path(out)
    protocol = json.loads(protocol_path.read_text(encoding='utf-8'))
    check_original(original, protocol)
    if out.exists():
        raise FileExistsError('Use a fresh output directory; an existing analysis is never overwritten.')
    data = load_inputs(original / 'dataset/dataset.npz')
    x, split, sessions, ids = (data[k] for k in ('x', 'split', 'session', 'sample_id'))
    tr, te = split == 'train', split == 'test'
    if set(sessions[tr]) & set(sessions[te]):
        raise ValueError('Shared train/test root')
    if not valid_counts(x, protocol['calls_per_bin']):
        raise ValueError('Invalid original count representation')
    selected = []
    for root in np.unique(sessions[te]):
        candidates = np.flatnonzero(te & (sessions == root))
        selected.extend(sorted(candidates, key=lambda i: str(ids[i]))[:protocol['max_examples_per_root']])
    selected = np.array(selected)
    query, qids, qsessions = x[selected], ids[selected], sessions[selected]
    old_refs = json.loads((original / 'experiment/occlusion-references.json').read_text(encoding='utf-8'))
    policies, plan = make_plan(query, x[tr], sessions[tr], ids[tr], old_refs['medoid_sample_id'], protocol)
    fixed = x[tr][np.flatnonzero(ids[tr] == old_refs['medoid_sample_id'])[0]]
    if not np.array_equal(fixed, np.array(old_refs['arrays']['training_historical_medoid'])):
        raise ValueError('Historical reference differs from the original training window')
    out.mkdir(parents=True)
    save_json(out / 'selection-plan.json', {'policies': policies, 'query_ids': qids.tolist(),
        'query_sessions': qsessions.tolist(), 'training_ids': ids[tr].tolist(),
        'training_roots': sessions[tr].tolist(), 'donor_indexes': plan.tolist()})
    old_freeze = json.loads((original / 'experiment/freeze.json').read_text())
    checkpoint = next(r for r in old_freeze['checkpoints'] if r['model'] == 'stgnn' and r['seed'] == old_freeze['selected_stgnn_seed'])
    weights_path = original / 'experiment/checkpoints' / checkpoint['checkpoint']
    if sha(weights_path) != checkpoint['checkpoint_sha256']:
        raise ValueError('Weights changed')
    code_files = [{'name': p.name, 'sha256': sha(p)} for p in sorted(Path(__file__).parent.glob('*.py'))]
    frozen = {'protocol_sha256': sha(protocol_path), 'original_manifest_sha256': protocol['original_manifest_sha256'],
              'checkpoint': checkpoint, 'selection_plan_sha256': sha(out / 'selection-plan.json'),
              'source': code_files, 'stage': 'Plan and donors fixed before any new model prediction',
              'target_fields_read': False, 'retraining': False, 'new_independent_test': False}
    save_json(out / 'freeze.json', frozen)
    torch.set_num_threads(protocol['threads'])
    torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    old_protocol = json.loads((original / 'protocol.json').read_text())
    graph = np.array(json.loads((original / 'experiment/graph.json').read_text())['adjacency'], dtype=np.float32)
    model = model_for('stgnn', graph, old_protocol)
    model.load_state_dict(torch.load(weights_path, map_location='cpu', weights_only=True))
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    original_pred, importance, perturbations = evaluate(
        lambda values: predict(model, values), query, x[tr], plan, policies, qids, qsessions, ids[tr], protocol)
    with np.load(original / 'experiment/test-predictions.npz', allow_pickle=False) as old_predictions:
        lookup = {sid: i for i, sid in enumerate(old_predictions['sample_id'])}
        expected = old_predictions[f"stgnn-{old_freeze['selected_stgnn_seed']}"][[lookup[sid] for sid in qids]]
    if not np.allclose(original_pred, expected, atol=1e-6, rtol=0):
        raise ValueError('Frozen model differs from original predictions')
    # CPU kernels can depend on batch shape (120 selected versus the original
    # batch of 512). Compare within 1e-6 here and report the actual maximum.
    rankings, vectors = comparisons(importance, policies, qids, qsessions, protocol)
    summary = summarize(rankings, perturbations, importance, policies, qsessions, protocol)
    save_json(out / 'originals.json', [{'sample_id': str(qids[i]), 'session': str(qsessions[i]),
              'input': query[i].tolist(), 'prediction': original_pred[i].tolist()} for i in range(len(query))])
    save_json(out / 'perturbations.json', perturbations)
    save_json(out / 'rankings.json', rankings)
    save_json(out / 'summary.json', summary)
    np.savez_compressed(out / 'importance.npz', sample_id=qids, session=qsessions, **vectors)
    for record in code_files:
        if sha(Path(__file__).parent / record['name']) != record['sha256']:
            raise ValueError('Analysis source changed during execution')
    if sha(out / 'selection-plan.json') != frozen['selection_plan_sha256'] or sha(weights_path) != checkpoint['checkpoint_sha256']:
        raise ValueError('Frozen selection or weights changed')
    original_count = check_original(original, protocol)
    save_json(out / 'completed.json', {'original_artifacts_unchanged': original_count,
              'all_perturbations_valid': True, 'original_prediction_max_abs_difference': float(np.abs(original_pred-expected).max()),
              'summary_sha256': sha(out / 'summary.json'), 'perturbations_sha256': sha(out / 'perturbations.json'),
              'rankings_sha256': sha(out / 'rankings.json'), 'frozen_plan_and_source_unchanged': True})
    print(json.dumps({'examples': len(query), 'policies': len(policies), 'perturbations': len(perturbations),
                      'all_valid': True, 'original_prediction_max_abs_difference': float(np.abs(original_pred-expected).max())}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    run(args.original, args.protocol, args.out)
