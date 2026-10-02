"""Frozen-model perturbations; reference sensitivity, not causal explanations."""
from itertools import combinations

import numpy as np
from scipy.stats import spearmanr

from .data import FAMILIES
from .models import predict


def references(x, sessions, ids):
    means = [x[sessions == sid].mean(axis=(0, 1)) for sid in np.unique(sessions)]
    mean = np.mean(means, axis=0)
    full_mean = np.broadcast_to(mean, x.shape[1:]).copy()
    distance = ((x - full_mean) ** 2).mean(axis=(1, 2))
    order = np.lexsort((ids, distance))
    index = int(order[0])
    return {'zero': np.zeros_like(full_mean), 'training_mean_equal_root': full_mean,
            'training_historical_medoid': x[index].copy()}, str(ids[index])


def masks(steps, nodes):
    result = []
    for node, name in enumerate(FAMILIES[:nodes]):
        mask = np.zeros((steps, nodes), dtype=bool)
        mask[:, node] = True
        result.append(('family', name, mask))
    for start in range(0, steps, 2):
        mask = np.zeros((steps, nodes), dtype=bool)
        mask[start:start+2] = True
        result.append(('time', f'bins_{start+1}_{min(start+2,steps)}', mask))
    return result


def perturb(x, mask, baseline):
    return np.where(mask[None], baseline[None], x)


def rank_comparison(a, b, k=3):
    """Undefined if there is no ranking information. Include all ties at cutoff."""
    if np.ptp(a) <= 1e-8 or np.ptp(b) <= 1e-8:
        rho = None
    else:
        rho = float(spearmanr(a, b).statistic)
    def top(v):
        if max(v) <= 1e-8:
            return set()
        threshold = sorted(v, reverse=True)[min(k, len(v))-1]
        return {i for i, value in enumerate(v) if value >= threshold - 1e-8}
    sa, sb = top(a), top(b)
    overlap = len(sa & sb) / len(sa | sb) if sa and sb else None
    return {'spearman': rho, 'top3_jaccard_with_ties': overlap}


def explain(model, x, y, sessions, ids, baseline_arrays, protocol):
    chosen = []
    limit = protocol['occlusion']['max_per_test_root']
    for sid in np.unique(sessions):
        indexes = np.flatnonzero(sessions == sid)
        chosen.extend(sorted(indexes, key=lambda i: str(ids[i]))[:limit])
    chosen = np.array(chosen, dtype=int)
    original = predict(model, x[chosen])
    original_loss = np.abs(original - y[chosen]).mean(-1)
    mask_list = masks(x.shape[1], x.shape[2])
    importance, rows, structural = {}, [], []
    for name, ref in baseline_arrays.items():
        scores = []
        for kind, label, mask in mask_list:
            altered = perturb(x[chosen], mask, ref)
            pred = predict(model, altered)
            delta = pred - original
            score = np.abs(delta).mean(-1)
            scores.append(score)
            broken = ~np.isclose(altered.sum(-1), protocol['calls_per_bin'], atol=1e-5)
            fractional = ~np.isclose(altered, np.round(altered), atol=1e-5)
            structural.append({'reference': name, 'mask_kind': kind, 'mask': label,
                               'examples': len(chosen), 'examples_with_invalid_bin_total': int(broken.any(-1).sum()),
                               'examples_with_fractional_counts': int(fractional.any(axis=(1, 2)).sum())})
            for j, i in enumerate(chosen):
                rows.append({'sample_id': str(ids[i]), 'session': str(sessions[i]), 'reference': name,
                             'mask_kind': kind, 'mask': label, 'importance': float(score[j]),
                             'prediction_delta': delta[j].tolist(),
                             'mae_delta': float(np.abs(pred[j] - y[i]).mean() - original_loss[j]),
                             'invalid_bin_total': bool(broken[j].any()),
                             'fractional_input_counts': bool(fractional[j].any())})
        importance[name] = np.stack(scores, axis=1)
    comparisons = []
    for left, right in combinations(baseline_arrays, 2):
        for kind in ('family', 'time'):
            cols = [i for i, (k, _, _) in enumerate(mask_list) if k == kind]
            for j, i in enumerate(chosen):
                comparisons.append({'sample_id': str(ids[i]), 'session': str(sessions[i]), 'mask_kind': kind,
                                    'left': left, 'right': right,
                                    **rank_comparison(importance[left][j, cols], importance[right][j, cols])})
    originals = [{'sample_id': str(ids[i]), 'session': str(sessions[i]), 'prediction': original[j].tolist(),
                  'target': y[i].tolist(), 'input': x[i].tolist()} for j, i in enumerate(chosen)]
    return {'originals': originals, 'perturbations': rows, 'rank_comparisons': comparisons,
            'structural_checks': structural, 'model_interpretation_only': True,
            'note': 'Same inputs, masks, graph and weights; only reference changes. Perturbed inputs need not be plausible observations.'}
