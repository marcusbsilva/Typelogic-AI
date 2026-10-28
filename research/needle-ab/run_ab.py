"""Offline A/B test: do extracted fields change the answers of the TypeLogic engines?

    python research/needle-ab/run_ab.py [--cases research/needle-ab/cases.json] [--engines generalist,specialist]

Every decision goes through Application.evaluate, exactly as in the web app, in five variants per document:
  none      the scenario as plain text (no fields)
  format    the fields attached with every value empty: same JSON wrapper, no information (format-only control)
  needle    the fields filled by Needle 3 on this computer
  oracle    the fields filled with the values a large teacher model copied from the document (only values written in it)
  corrupt   the oracle values altered (numbers x1.5, dates +7 days, texts swapped): wrong extraction control
Metrics against the consensus targets of typelogic-distill-v1-test2: accuracy (argmax), cross-entropy against the soft
target (NLL) and paired differences against "none" with a bootstrap 95% interval. No network is used.
"""
import argparse
from datetime import date, timedelta
import json
import math
from pathlib import Path
import random
import shutil
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from typelogic.offline import install  # noqa: E402
from typelogic import modes, needle  # noqa: E402
from typelogic.profiles import active_model_dir  # noqa: E402
from typelogic.server import Application  # noqa: E402

VARIANTS = ('none', 'format', 'needle', 'oracle', 'corrupt')


def attach(fields, values, text):
    rows = []
    for f in fields:
        v = values.get(f['name'])
        rows.append({'name': f['name'], 'type': f['type'], 'value': v,
                     'check': 'not_stated' if v is None else needle.evidence(v, f['type'], text)})
    return {'engine': 'needle3', 'fields': rows}


def corrupt(fields, values, rng, pool):
    out = {}
    for f in fields:
        v = values.get(f['name'])
        if v is None:
            out[f['name']] = None
        elif f['type'] == 'boolean':
            out[f['name']] = not v
        elif f['type'] == 'integer':
            out[f['name']] = int(v) + max(1, abs(int(v)) // 2)
        elif f['type'] == 'number':
            out[f['name']] = round(v * 1.5 + (1 if v == 0 else 0), 2)
        elif f['type'] == 'date':
            out[f['name']] = (date.fromisoformat(v) + timedelta(days=7)).isoformat()
        else:
            choices = [p for p in pool if p.lower() != str(v).lower()]
            out[f['name']] = rng.choice(choices) if choices else v
    return out


def ce(target, probs):
    return -sum(t * math.log(max(probs.get(k, 0.0), 1e-12)) for k, t in target.items() if t > 0)


def argmax(d):
    return max(d, key=d.get)


def bootstrap(diffs, rng, n=2000):
    if not diffs:
        return None
    means = sorted(statistics.fmean(rng.choice(diffs) for _ in diffs) for _ in range(n))
    return [round(means[int(0.025 * n)], 4), round(means[int(0.975 * n)], 4)]


def summarize(rows, rng, variants=VARIANTS, reference='none'):
    out = {}
    base = {(r['doc'], r['qid']): r for r in rows if r['variant'] == reference and r.get('probabilities')}
    for v in variants:
        sel = [r for r in rows if r['variant'] == v and r.get('probabilities') and (r['doc'], r['qid']) in base]
        if not sel:
            continue
        acc = [argmax(r['probabilities']) == argmax(r['target']) for r in sel]
        nll = [ce(r['target'], r['probabilities']) for r in sel]
        item = {'questions': len(sel), 'accuracy': round(statistics.fmean(acc), 4), 'nll': round(statistics.fmean(nll), 4),
                'mean_top_probability': round(statistics.fmean(max(r['probabilities'].values()) for r in sel), 4)}
        if v != reference:
            pairs = [(base[(r['doc'], r['qid'])], r) for r in sel]
            gained = sum(argmax(b['probabilities']) != argmax(b['target']) and argmax(r['probabilities']) == argmax(r['target']) for b, r in pairs)
            lost = sum(argmax(b['probabilities']) == argmax(b['target']) and argmax(r['probabilities']) != argmax(r['target']) for b, r in pairs)
            d_acc = [(argmax(r['probabilities']) == argmax(r['target'])) - (argmax(b['probabilities']) == argmax(b['target'])) for b, r in pairs]
            d_nll = [ce(r['target'], r['probabilities']) - ce(b['target'], b['probabilities']) for b, r in pairs]
            item.update(**{'vs_' + reference: {'answers_fixed': gained, 'answers_broken': lost,
                                 'accuracy_diff': round(statistics.fmean(d_acc), 4), 'accuracy_diff_ci95': bootstrap(d_acc, rng),
                                 'nll_diff': round(statistics.fmean(d_nll), 4), 'nll_diff_ci95': bootstrap(d_nll, rng)}})
        out[v] = item
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--cases', type=Path, default=ROOT / 'research/needle-ab/cases.json')
    p.add_argument('--engines', default='generalist,specialist')
    p.add_argument('--specialist-device', default='xpu', choices=('xpu', 'cpu'))
    p.add_argument('--out', type=Path, default=ROOT / 'research/needle-ab/results.json')
    args = p.parse_args()
    install()
    data = json.loads(args.cases.read_text(encoding='utf-8'))
    cases = data['cases']
    rng = random.Random(7)
    pool = sorted({str(v) for c in cases for f in c['fields'] if f['type'] == 'text' for v in [c['oracle'].get(f['name'])] if v})

    runtime = needle.Runtime()
    status = runtime.status()
    if not status['available']:
        raise SystemExit(status['reason'])
    cache = args.out.with_name('needle-extraction.json')   # Needle values per document (reused by later runs)
    extraction = json.loads(cache.read_text(encoding='utf-8')) if cache.is_file() else {}
    started = time.perf_counter()
    for c in (c for c in cases if c['id'] not in extraction):
        try:
            r = runtime.extract(c['state'], c['fields'])
            extraction[c['id']] = {'values': {f['name']: f['value'] for f in r['fields']}, 'seconds': r['seconds'], 'confidence': r['confidence'],
                                   'withheld': r['withheld_by_engine']}
        except (ValueError, RuntimeError) as exc:
            extraction[c['id']] = {'values': {}, 'error': str(exc)}
    extraction_seconds = time.perf_counter() - started
    cache.write_text(json.dumps(extraction, ensure_ascii=False, indent=1), encoding='utf-8')
    agree = filled = total = 0
    for c in cases:
        for f in c['fields']:
            total += 1
            got, want = extraction[c['id']]['values'].get(f['name']), c['oracle'].get(f['name'])
            filled += got is not None
            agree += got is not None and want is not None and (got == want or isinstance(got, str) and isinstance(want, str) and got.strip().lower() == want.strip().lower()
                                                              or isinstance(got, (int, float)) and isinstance(want, (int, float)) and not isinstance(got, bool) and abs(got - want) < 1e-6)
    print(f'needle: {filled}/{total} fields filled, {agree} equal to the oracle, {extraction_seconds:.0f} s', file=sys.stderr, flush=True)

    settings = modes.load_settings(Path.home() / '.typelogic-ai')
    engines = {}
    wanted = args.engines.split(',')
    if 'generalist' in wanted:
        from typelogic import ollama
        print('Ollama:', ollama.ensure_running(settings['ollama_model'], models_dir=settings['ollama_models']), file=sys.stderr, flush=True)
        engines['generalist'] = modes.build('hybrid', active_model_dir(ROOT), settings).generalist
    if 'specialist' in wanted:
        engines['specialist'] = modes.build('gpu' if args.specialist_device == 'xpu' else 'cpu', active_model_dir(ROOT), settings)
    rows = []
    timing = {}
    for name, engine in engines.items():
        if name == 'specialist' and args.specialist_device == 'xpu':
            from typelogic import ollama
            ollama.unload_all()   # the generalist in Ollama shares the GPU memory
            time.sleep(3)
        engine.load()
        folder = Path(tempfile.mkdtemp(prefix='needle-ab-'))
        app = Application(ROOT, folder, engine, mode='custom')
        t0 = time.perf_counter()
        for c in cases:
            values = {'none': None, 'format': {}, 'needle': extraction[c['id']]['values'], 'oracle': c['oracle'],
                      'corrupt': corrupt(c['fields'], c['oracle'], rng, pool)}
            for variant in VARIANTS:
                payload = {'title': 'A/B', 'state': c['state'], 'questions': c['questions'], 'threshold': 0.85,
                           'permutation_check': False, 'use_memory': False, 'exact_checks': []}
                if values[variant] is not None:
                    payload['extracted_fields'] = attach(c['fields'], values[variant], c['state'])
                try:
                    response = app.evaluate(payload)
                    for qid, a in response['answers'].items():
                        rows.append({'engine': name, 'variant': variant, 'doc': c['id'], 'qid': qid, 'type': a['type'], 'language': c['language'],
                                     'target': c['targets'][qid], 'probabilities': a['probabilities']})
                except Exception as exc:  # noqa: BLE001 - an over-length state is recorded, not fatal
                    for qid in c['questions']:
                        rows.append({'engine': name, 'variant': variant, 'doc': c['id'], 'qid': qid, 'error': str(exc)[:200]})
            print(f'{name}: {c["id"]} done', file=sys.stderr, flush=True)
        timing[name] = round(time.perf_counter() - t0, 1)
        app.close()
        shutil.rmtree(folder, ignore_errors=True)
    report = {'source': data['source'], 'documents': len(cases), 'questions': data['questions'], 'teacher': data['teacher'],
              'needle': {'fields': total, 'filled': filled, 'equal_to_oracle': agree, 'seconds': round(extraction_seconds, 1)},
              'oracle_values_written_in_text': sum(v is not None for c in cases for v in c['oracle'].values()),
              'timing_seconds': timing, 'errors': sum('error' in r for r in rows),
              'results': {name: summarize([r for r in rows if r['engine'] == name], rng) for name in engines},
              'by_language': {name: {lang: summarize([r for r in rows if r['engine'] == name and r.get('language') == lang], rng) for lang in ('en', 'pt')} for name in engines}}
    args.out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    args.out.with_name(args.out.stem + '-rows.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    for name in engines:
        for v, item in report['results'][name].items():
            print(name, v, json.dumps(item), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
