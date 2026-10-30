"""Spike report: accuracy, chance-corrected skill, calibration, latency, memory and parity with the PyTorch reference.

    python report.py --v1 <TypeLogic-AI> --results results --banking 1000

Reads results/<target>/<suite>.jsonl (+ target.json) and writes results/REPORT.md and results/summary.json.
An item that failed (error, over-length state) counts as wrong: nothing is dropped.
Gates (docs/SPIKES.md): specialist GGUF parity with PyTorch, quantization loss, generalist choice.
"""
import argparse
import json
import math
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import suites  # noqa: E402

REFERENCE = 'torch'
ORDER = ['torch', 'spec-f16', 'spec-q8', 'spec-q8-fix', 'spec-q4', 'kev4b-q4', 'clef-flash-q4', 'plumb-q8', 'plumb-q4',
         'trio-spec-q8', 'trio-plumb-q4', 'trio-clef-flash-q4', 'trio-clef-flash-q4km-b', 'trio-clef-flash-iq4xs-b',
         'app-generalist', 'app-specialist', 'app-clef', 'app-auto']


def wilson(k, n, z=1.96):
    if not n:
        return [0.0, 0.0]
    phat = k / n
    centre = (phat + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(centre - half, 4), round(centre + half, 4)]


def rows_of(path):
    return {r['id']: r for r in (json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip())} if path.is_file() else {}


def metrics(items, rows, reference=None):
    q_total = correct = valid = 0
    skill, brier, conf_bins, latency = [], [], [[] for _ in range(10)], []
    agree = agree_n = 0
    diffs = []
    by_type = {}
    for item in items:
        row = rows.get(item['id'])
        if row and 'latency_s' in row and 'dist' in row:
            latency.append(row['latency_s'])
        for qid, q in item['questions'].items():
            keys = suites.canonical_keys(q)
            gold = item['gold'][qid]
            q_total += 1
            t = by_type.setdefault(q['type'], [0, 0])
            t[1] += 1
            dist = (row or {}).get('dist', {}).get(qid)
            if not dist:
                skill.append(-1 / (len(keys) - 1))     # a failure scores as a wrong answer
                continue
            valid += 1
            best = max(range(len(keys)), key=dist.__getitem__)
            ok = keys[best] == gold
            correct += ok
            t[0] += ok
            k = len(keys)
            skill.append(((1.0 if ok else 0.0) - 1 / k) / (1 - 1 / k))
            brier.append(sum((p - (1.0 if key == gold else 0.0)) ** 2 for p, key in zip(dist, keys)))
            conf_bins[min(9, int(dist[best] * 10))].append((dist[best], ok))
            ref = (reference or {}).get(item['id'], {}).get('dist', {}).get(qid) if reference is not None else None
            if ref:
                agree_n += 1
                agree += max(range(len(keys)), key=ref.__getitem__) == best
                diffs.append(max(abs(a - b) for a, b in zip(dist, ref)))
    n_conf = sum(len(b) for b in conf_bins)
    ece = sum(len(b) / n_conf * abs(statistics.fmean(c for c, _ in b) - statistics.fmean(o for _, o in b)) for b in conf_bins if b) if n_conf else None
    latency.sort()
    out = {'questions': q_total, 'valid': valid, 'correct': correct,
           'accuracy': round(correct / q_total, 4) if q_total else None, 'accuracy_wilson_95': wilson(correct, q_total),
           'skill': round(statistics.fmean(skill), 4) if skill else None,
           'brier': round(statistics.fmean(brier), 4) if brier else None, 'ece_10': round(ece, 4) if ece is not None else None,
           'latency_p50_s': round(latency[len(latency) // 2], 4) if latency else None,
           'latency_p95_s': round(latency[min(len(latency) - 1, int(.95 * len(latency)))], 4) if latency else None,
           'by_type': {k: {'accuracy': round(v[0] / v[1], 4), 'questions': v[1]} for k, v in sorted(by_type.items())}}
    if reference is not None and agree_n:
        out['parity'] = {'compared': agree_n, 'argmax_agreement': round(agree / agree_n, 4),
                         'mean_max_abs_diff': round(statistics.fmean(diffs), 4), 'max_abs_diff': round(max(diffs), 4)}
    return out


def gates(summary):
    """Pass/fail of the spike gates; None when a target is missing."""
    def get(target, suite, *path):
        v = summary.get(target, {}).get('suites', {}).get(suite)
        for p in path:
            v = (v or {}).get(p)
        return v
    out = {}
    agreements = [get('spec-q8', s, 'parity', 'argmax_agreement') for s in ('jevbench', 'test2')]
    if all(a is not None for a in agreements):
        out['G1 specialist Q8 = PyTorch (argmax agreement >= 0.99 on jevbench and test2)'] = all(a >= 0.99 for a in agreements)
    for q in ('spec-q4',):
        drops = [(get('spec-q8', s, 'accuracy') or 0) - (get(q, s, 'accuracy') or 0) for s in ('jevbench', 'test2', 'banking77')
                 if get(q, s, 'accuracy') is not None and get('spec-q8', s, 'accuracy') is not None]
        if drops:
            out['G2 specialist Q4 loses <= 1.0 point vs Q8 on every suite'] = max(drops) <= 0.01
    for q in ('plumb-q4',):
        drops = [(get('plumb-q8', s, 'accuracy') or 0) - (get(q, s, 'accuracy') or 0) for s in ('jevbench', 'test2')
                 if get(q, s, 'accuracy') is not None and get('plumb-q8', s, 'accuracy') is not None]
        if drops:
            out['G3 Plumb Q4 loses <= 1.0 point vs Q8 on jevbench and test2'] = max(drops) <= 0.01
    clef, plumb = [get('clef-flash-q4', s, 'skill') for s in ('jevbench', 'test2')], [get('plumb-q8', s, 'skill') for s in ('jevbench', 'test2')]
    if all(v is not None for v in clef + plumb):
        out['G4 Clef-Flash beats Plumb Q8 on chance-corrected skill (jevbench and test2)'] = all(c > p for c, p in zip(clef, plumb))
    return out


# trio checks: (candidate, baseline measured alone, suites, max accuracy loss in points, answers must not change)
TRIO_PAIRS = [
    ('trio-spec-q8', 'spec-q8-fix', ('jevbench', 'test2', 'banking77'), 0.5, True),
    ('trio-plumb-q4', 'plumb-q4', ('jevbench', 'test2'), 0.5, False),
    ('trio-clef-flash-q4', 'clef-flash-q4', ('jevbench', 'test2', 'banking77'), 1.0, False),
    ('trio-clef-flash-q4km-b', 'clef-flash-q4', ('jevbench', 'test2', 'banking77'), 1.0, False),
    ('trio-clef-flash-iq4xs-b', 'clef-flash-q4', ('jevbench', 'test2', 'banking77'), 1.0, False),
]
# TypeLogic.exe (POST /v1/decide, spike/via_app.py) against the same models measured by the spike with the same settings
APP_PAIRS = [
    ('app-generalist', 'trio-plumb-q4', ('jevbench', 'test2'), 0.5, False),
    ('app-specialist', 'trio-spec-q8', ('jevbench', 'test2', 'banking77'), 0.5, True),
    ('app-clef', 'trio-clef-flash-iq4xs-b', ('jevbench', 'test2', 'banking77'), 0.5, False),
    ('app-auto', 'trio-plumb-q4', ('jevbench', 'test2'), 0.5, False),
]
MIN_FREE_MIB = 256       # device memory left with the three models loaded (router --models-memory-margin)
MAX_SLOWER = 0.20        # a p50 latency 20% above the model alone is flagged


def paired(items, cand, base):
    """Same questions, two runs -> accuracy of each, changed answers, largest probability difference, p50 latencies."""
    n = k_c = k_b = changed = 0
    largest = 0.0
    for item in items:
        rc, rb = cand.get(item['id'], {}), base.get(item['id'], {})
        for qid, q in item['questions'].items():
            keys = suites.canonical_keys(q)
            n += 1
            dc, db = rc.get('dist', {}).get(qid), rb.get('dist', {}).get(qid)
            ac = keys[max(range(len(keys)), key=dc.__getitem__)] if dc else None
            ab = keys[max(range(len(keys)), key=db.__getitem__)] if db else None
            k_c += ac == item['gold'][qid]
            k_b += ab == item['gold'][qid]
            changed += ac != ab
            if dc and db:
                largest = max(largest, max(abs(x - y) for x, y in zip(dc, db)))
    def p50(rows):
        v = sorted(r['latency_s'] for r in rows.values() if 'dist' in r)
        return v[len(v) // 2] if v else None
    return {'questions': n, 'accuracy': k_c / n if n else None, 'baseline_accuracy': k_b / n if n else None,
            'delta_points': 100 * (k_c - k_b) / n if n else None, 'changed_answers': changed, 'max_abs_dp': largest,
            'p50_s': p50(cand), 'baseline_p50_s': p50(base)}


def pair_lines(results, data, pairs):
    """PASS/FAIL lines of question-by-question comparisons (accuracy loss, changed answers); latency is only flagged."""
    lines, out = [], {}
    for cand, base, names, max_loss, exact in pairs:
        if not (results / cand).is_dir() or not (results / base).is_dir():
            continue
        for s in names:
            if not (results / cand / f'{s}.jsonl').is_file() or not (results / base / f'{s}.jsonl').is_file():
                continue
            r = paired(data[s], rows_of(results / cand / f'{s}.jsonl'), rows_of(results / base / f'{s}.jsonl'))
            ok = r['delta_points'] >= -max_loss and (r['changed_answers'] == 0 or not exact)
            slower = r['p50_s'] and r['baseline_p50_s'] and r['p50_s'] > (1 + MAX_SLOWER) * r['baseline_p50_s']
            r.update(passed=ok, slower=bool(slower))
            out[f'{cand}/{s}'] = r
            rule = f'loss <= {max_loss} pt' + (', no changed answer' if exact else '')
            lines.append(f"| {'PASS' if ok else 'FAIL'} | {cand} vs {base} | {s} | {100 * r['accuracy']:.1f}% vs {100 * r['baseline_accuracy']:.1f}% "
                         f"({r['delta_points']:+.1f} pt) | {r['changed_answers']} | {r['max_abs_dp']:.4f} | {r['p50_s']:.3f} vs {r['baseline_p50_s']:.3f} s"
                         f"{' (slower)' if slower else ''} | {rule} |")
    return lines, out


def poe_rows(gen, other, weight):
    """The combined mode computed from two runs measured alone (hybrid.product_of_experts of 1.x)."""
    out = {}
    for rid, rg in gen.items():
        ro = other.get(rid, {})
        if 'dist' not in rg or 'dist' not in ro:
            continue
        dist = {}
        for qid, g in rg['dist'].items():
            o = ro['dist'].get(qid)
            if not o:
                continue
            logs = [weight * math.log(max(a, 1e-12)) + (1 - weight) * math.log(max(b, 1e-12)) for a, b in zip(g, o)]
            top = max(logs)
            e = [math.exp(v - top) for v in logs]
            dist[qid] = [v / sum(e) for v in e]
        out[rid] = {'id': rid, 'dist': dist, 'latency_s': rg.get('latency_s', 0) + ro.get('latency_s', 0)}
    return out


ENSEMBLE = ('app-ensemble', 'trio-plumb-q4', 'trio-clef-flash-iq4xs-b', 0.5)


def ensemble_lines(results, data):
    cand, gen, other, weight = ENSEMBLE
    lines, out = [], {}
    if not all((results / d).is_dir() for d in (cand, gen, other)):
        return lines, out
    for s in ('jevbench', 'test2'):
        files = [results / d / f'{s}.jsonl' for d in (cand, gen, other)]
        if not all(f.is_file() for f in files):
            continue
        rc, rg, ro = (rows_of(f) for f in files)
        base = poe_rows(rg, ro, weight)
        r = paired(data[s], rc, base)
        mc, mb = metrics(data[s], rc), metrics(data[s], base)
        r.update(brier=mc['brier'], baseline_brier=mb['brier'], ece=mc['ece_10'], passed=r['delta_points'] >= -0.5)
        out[f'{cand}/{s}'] = r
        lines.append(f"| {'PASS' if r['passed'] else 'FAIL'} | {cand} vs product of experts ({gen} x {other}, w = {weight}) | {s} | "
                     f"{100 * r['accuracy']:.1f}% vs {100 * r['baseline_accuracy']:.1f}% ({r['delta_points']:+.1f} pt), Brier {mc['brier']} vs {mb['brier']} | "
                     f"{r['changed_answers']} | {r['max_abs_dp']:.4f} | {r['p50_s']:.3f} vs {r['baseline_p50_s']:.3f} s | loss <= 0.5 pt |")
    return lines, out


def app_checks(results, data):
    lines, out = pair_lines(results, data, APP_PAIRS)
    more, extra = ensemble_lines(results, data)
    lines += more
    out.update(extra)
    path = results / 'app-verify.json'
    if path.is_file():
        v = json.loads(path.read_text(encoding='utf-8'))
        out['verify'] = v
        for name, ok, detail in v.get('checks', []):
            lines.append(f"| {'PASS' if ok else 'FAIL'} | {name} | - | {detail} | - | - | - | - |")
    phase3 = results / 'phase3.json'
    if phase3.is_file():
        v = json.loads(phase3.read_text(encoding='utf-8'))
        out['phase3'] = {k: v.get(k) for k in ('checks', 'evaluations')}
        for name, ok, detail in v.get('checks', []):
            lines.append(f"| {'PASS' if ok else 'FAIL'} | phase 3: {name} | - | {detail} | - | - | - | - |")
    rel = results / 'release-verify.json'
    if rel.is_file():
        v = json.loads(rel.read_text(encoding='utf-8'))
        out['release'] = v.get('checks')
        for name, ok, detail in v.get('checks', []):
            lines.append(f"| {'PASS' if ok else 'FAIL'} | release: {name} | - | {detail} | - | - | - | - |")
    smoke = results / 'ui-smoke.json'
    if smoke.is_file():
        v = json.loads(smoke.read_text(encoding='utf-8'))
        out['ui'] = v.get('checks')
        for name, ok, detail in v.get('checks', []):
            lines.append(f"| {'PASS' if ok else 'FAIL'} | UI API: {name} | - | {detail} | - | - | - | - |")
    if lines:
        lines = ['', '## TypeLogic.exe against the models alone', '',
                 '| result | comparison | suite | accuracy | changed answers | max abs dp | p50 latency | rule |',
                 '|---|---|---|---|---|---|---|---|'] + lines
    return lines, out


def trio_checks(results, data):
    """-> (markdown lines, dict). Every check is PASS/FAIL; latency is reported and flagged, not a failure."""
    lines, out = pair_lines(results, data, TRIO_PAIRS)
    for path in sorted(results.glob('trio-*.json')):
        t = json.loads(path.read_text(encoding='utf-8'))
        used = {k: v.get('memory', {}).get('total_device_mib') for k, v in t.get('servers', {}).items()}
        free = (t.get('free_all_loaded') or {}).get('free_mib')
        before = (t.get('free_before') or {}).get('free_mib')
        if before is not None and used and None not in used.values():
            # the free figure another process reads can miss the other servers' allocations (Vulkan budget is per
            # process on Windows): use the free memory before loading minus what the three servers report
            estimate = round(before - sum(used.values()))
            free = estimate if free is None else min(free, estimate)
        stress = t.get('stress') or {}
        ok_mem = free is not None and free >= MIN_FREE_MIB and not t.get('error')
        ok_stress = bool(stress) and stress.get('all_alive') and not any((stress.get('errors') or {}).values())
        out[path.stem] = {'free_mib': free, 'device_mib': used, 'memory_ok': ok_mem, 'stress_ok': ok_stress, 'error': t.get('error'),
                          'stress': stress}
        lines.append(f"| {'PASS' if ok_mem else 'FAIL'} | {path.stem}: three models loaded | - | free {free} MiB (models {used}) | - | - | - | "
                     f">= {MIN_FREE_MIB} MiB free{'; error: ' + t['error'] if t.get('error') else ''} |")
        if stress:
            lines.append(f"| {'PASS' if ok_stress else 'FAIL'} | {path.stem}: concurrent stress | test2 x{stress.get('records')} | "
                         f"wall {stress.get('wall_seconds')} s, errors {stress.get('errors')} | - | - | median {stress.get('median_latency_s')} | "
                         f"no error, all servers alive |")
    if lines:
        lines = ['', '## Three models at once (trio)', '',
                 '| result | comparison | suite | accuracy | changed answers | max abs dp | p50 latency | rule |',
                 '|---|---|---|---|---|---|---|---|'] + lines
    return lines, out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--v1', type=Path, required=True)
    p.add_argument('--results', type=Path, default=HERE / 'results')
    p.add_argument('--banking', type=int, default=1000)
    p.add_argument('--limit', type=int, help='smoke test: first N items of every suite')
    args = p.parse_args()
    data = {s: suites.load(args.v1, s, (args.banking or None) if s == 'banking77' else None)[:args.limit] for s in suites.SUITES}
    targets = sorted([d for d in args.results.iterdir() if d.is_dir()], key=lambda d: (ORDER.index(d.name) if d.name in ORDER else 99, d.name))
    summary = {}
    for target in targets:
        entry = {'suites': {}}
        meta = target / 'target.json'
        if meta.is_file():
            info = json.loads(meta.read_text(encoding='utf-8'))
            entry.update(file=info.get('model'), gib=round(info.get('bytes', 0) / 2**30, 2), load_seconds=info.get('load_seconds'),
                         device_mib=info.get('memory', {}).get('total_device_mib'))
        for s, items in data.items():
            path = target / f'{s}.jsonl'
            if not path.is_file():
                continue
            ref = rows_of(args.results / REFERENCE / f'{s}.jsonl') if target.name != REFERENCE else None
            entry['suites'][s] = metrics(items, rows_of(path), ref)
        summary[target.name] = entry
    trio_lines, trio = trio_checks(args.results, data)
    app_lines, app = app_checks(args.results, data)
    result = {'summary': summary, 'gates': gates(summary), 'trio': trio, 'app': app}
    (args.results / 'summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')

    lines = ['# TypeLogic v2 spikes - results', '',
             'Accuracy counts failures as wrong. Skill = chance-corrected accuracy (0 = guessing, 1 = perfect).',
             f'Parity = agreement with the PyTorch reference ({REFERENCE}). BANKING77: first {args.banking or "all"} test rows.', '',
             '| target | file GiB | device MiB | load s | suite | accuracy (95% CI) | skill | Brier | ECE | p50 s | p95 s | argmax = torch | max abs dp |',
             '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for name, entry in summary.items():
        for s, m in entry['suites'].items():
            par = m.get('parity', {})
            ci = m['accuracy_wilson_95']
            lines.append(f"| {name} | {entry.get('gib', '')} | {entry.get('device_mib', '')} | {entry.get('load_seconds', '')} | {s} "
                         f"| {m['accuracy']:.3f} ({ci[0]:.3f}-{ci[1]:.3f}) | {m['skill']} | {m['brier']} | {m['ece_10']} | {m['latency_p50_s']} | {m['latency_p95_s']} "
                         f"| {par.get('argmax_agreement', '')} | {par.get('max_abs_diff', '')} |")
    lines += ['', '## Gates', '']
    lines += [f"- {'PASS' if ok else 'FAIL'}: {gate}" for gate, ok in result['gates'].items()] or ['- (no gate could be evaluated yet)']
    lines += trio_lines
    lines += app_lines
    (args.results / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
