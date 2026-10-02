"""Observed-call sequence windows; no interpolation of unobserved wall time."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

import numpy as np

FAMILIES = ('read', 'search', 'list', 'write_edit', 'shell', 'mcp', 'coordination', 'web_other')
CATEGORY = {'read': 'read', 'search': 'search', 'list': 'list', 'write': 'write_edit',
            'edit': 'write_edit', 'shell': 'shell', 'mcp': 'mcp', 'agent': 'coordination',
            'web': 'web_other', 'other': 'web_other', 'unknown': 'web_other'}


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def family(category):
    return FAMILIES.index(CATEGORY.get(category, 'web_other'))


def windows(rows, protocol):
    """Only complete observed sequences, separated by actor, epoch and invalid rows.

    Input and target boundaries have strictly increasing start times. Never fill
    a missing row/time interval with zero. Zeros mean no such family among the
    explicitly enumerated calls of a complete event-count block.
    """
    b, history, horizon = (protocol[k] for k in ('calls_per_bin', 'history_bins', 'target_bins'))
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row['session'], row['actor'], row['epoch'])].append(row)
    records, rejected = [], Counter()
    for key, items in sorted(grouped.items()):
        chunks, chunk = [], []
        for row in sorted(items, key=lambda r: r['seq']):
            if not row['eligible']:
                rejected['ineligible_calls'] += 1
                if chunk:
                    chunks.append(chunk)
                chunk = []
            else:
                chunk.append(row)
        if chunk:
            chunks.append(chunk)
        for chunk in chunks:
            bins = len(chunk) // b
            rejected['unbinned_tail_calls'] += len(chunk) % b
            if bins < history + horizon:
                rejected['short_chunks'] += 1
                continue
            counts = np.zeros((bins, len(FAMILIES)), dtype=np.float32)
            for i, row in enumerate(chunk[:bins * b]):
                counts[i // b, row['family']] += 1
            for stop in range(history, bins - horizon + 1, horizon):
                lo, hi = (stop - history) * b, (stop + horizon) * b
                # A tie across any block boundary does not establish which call
                # was available first. Do not break it with a fabricated order.
                if any(chunk[i - 1]['start_ns'] >= chunk[i]['start_ns'] for i in range(lo + b, hi, b)):
                    rejected['unordered_boundaries'] += 1
                    continue
                past, future = chunk[lo:stop * b], chunk[stop * b:hi]
                assert max(r['start_ns'] for r in past) < min(r['start_ns'] for r in future)
                sample_id = hashlib.sha256('|'.join(r['call_key'] for r in past + future).encode()).hexdigest()[:24]
                records.append({'id': sample_id, 'session': key[0], 'actor': key[1], 'epoch': key[2],
                                'x': counts[stop-history:stop], 'y': counts[stop:stop+horizon].sum(0),
                                'input_calls': [r['call_key'] for r in past], 'target_calls': [r['call_key'] for r in future],
                                'forecast_origin_ns': past[-1]['start_ns'], 'first_target_ns': future[0]['start_ns'],
                                'target_last_ns': future[-1]['start_ns'], 'coverage': 'complete_enumerated_observed_call_blocks',
                                'wall_clock_collection_coverage': 'unknown'})
    return records, dict(rejected)


def make_graph(x, sessions):
    """Training-input lag-one associations, equal weight per root session.

    A[i,j] mixes source family j into destination family i. This is an empirical
    predictive association, never an observed dependency or a causal edge.
    Only training INPUTS enter it; no validation/test values or target labels.
    """
    n = x.shape[-1]
    score = np.zeros((n, n), dtype=np.float64)
    for sid in np.unique(sessions):
        selected = x[sessions == sid]
        src = selected[:, :-1].reshape(-1, n)
        dst = selected[:, 1:].reshape(-1, n)
        product = dst.T @ src
        norm = np.sqrt((dst ** 2).sum(0)[:, None] * (src ** 2).sum(0)[None, :])
        score += np.divide(product, norm, out=np.zeros_like(product), where=norm > 0)
    score /= len(np.unique(sessions))
    np.fill_diagonal(score, 0)
    score += np.eye(n)
    return (score / score.sum(1, keepdims=True)).astype(np.float32)


def extract(manifest_path, protocol_path, out):
    # Imported only by the explicit offline research command. Never auto-import.
    from agentwatch.config import load_config
    from agentwatch.core.correlate import build_session

    protocol = json.loads(Path(protocol_path).read_text(encoding='utf-8'))
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    records, audits, call_rows = [], [], []
    for source in manifest['sessions']:
        path = Path(source['frozen_path'])
        assert sha(path) == source['frozen_sha256'], f'Frozen corpus changed: {path.name}'
        key = source['client'] + ':' + source['session_key']
        split = protocol['session_splits'][key]
        # Read the frozen file directly, never a live store or importer.
        events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        cfg = load_config(path.parents[3])
        view = build_session(events, cfg)
        rows = []
        for c in view.calls:
            row = {'session': key, 'actor': c.agent_key, 'epoch': c.context_epoch, 'seq': c.seq,
                   'call_key': c.key, 'start_ns': c.start_ns, 'family': family(c.category),
                   'eligible': bool(c.has_start and c.start_ns is not None and c.tool_name and not c.ambiguous
                                    and c.agent_key not in view.timing_unreliable_agents),
                   'event_ids': c.event_ids, 'source': c.evidence.get('source_start')}
            rows.append(row)
        samples, rejections = windows(rows, protocol)
        for sample in samples:
            sample['split'], sample['client'] = split, source['client']
        records.extend(samples)
        call_rows.extend(rows)
        audits.append({'session': key, 'split': split, 'frozen_path': str(path), 'sha256': source['frozen_sha256'],
                       'calls': len(rows), 'eligible_calls': sum(r['eligible'] for r in rows), 'windows': len(samples),
                       'rejections': rejections, 'timing_unreliable_actors': view.timing_unreliable_agents})
        print(json.dumps({'session': key, 'split': split, 'windows': len(samples)}), flush=True)
    assert records, 'No eligible sequences'
    x = np.stack([s.pop('x') for s in records])
    y = np.stack([s.pop('y') for s in records])
    splits = np.array([s['split'] for s in records])
    sessions = np.array([s['session'] for s in records])
    for split, minimum in (('train', 2), ('validation', 1), ('test', 1)):
        assert len(np.unique(sessions[splits == split])) >= minimum, f'Insufficient {split} roots'
    np.savez_compressed(out / 'dataset.npz', x=x, y=y, split=splits, session=sessions,
                        sample_id=np.array([s['id'] for s in records]), client=np.array([s['client'] for s in records]))
    save_json(out / 'windows.json', records)
    save_json(out / 'coverage.json', {'target': protocol['target'], 'families': FAMILIES, 'sessions': audits,
                                   'zero_semantics': 'Absence among enumerated observed calls only; wall-clock silences remain unknown.',
                                   'excluded_fields': ['tokens', 'outputs', 'parameters', 'resources', 'finding_labels', 'replacement_verdicts']})
    (out / 'call-references.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in call_rows), encoding='utf-8')
    save_json(out / 'identity.json', {'manifest_sha256': sha(manifest_path), 'protocol_sha256': sha(protocol_path),
                                    'dataset_sha256': sha(out / 'dataset.npz'),
                                    'coverage_sha256': sha(out / 'coverage.json'), 'windows_sha256': sha(out / 'windows.json')})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--protocol', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    extract(args.manifest, args.protocol, args.out)
