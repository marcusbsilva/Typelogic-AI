"""Offline measurement of the Case / Evidence / Decision format (phase 3) and of the evidence extractors (phase 2).

    python research/case-format-ab/run.py [--models qwen3.5:4b,qwen3.5:9b] [--engines generalist,specialist]

Uses the 33 documents (103 questions) and oracle evidence of research/needle-ab/cases.json.
Step 1, extractors: each Ollama model extracts the declared fields of every document through typelogic/evidence.py;
values are compared with the oracle (correct, wrong, omitted; wrong values the check flags). Results are cached in
extraction-<model>.json.
Step 2, formats, each decision through Application.evaluate:
  text            the document as plain text (today's format)
  case            v2: case.details = the document, no evidence (format change only)
  case_oracle     v2: the document + oracle evidence
  case_extracted  v2: the document + evidence from the best extractor of step 1
  oracle_only     v2: a one-line case summary + oracle evidence, without the document
  case_text*      the same as case, case_oracle and case_extracted with engine_input "text" (plain text with evidence
                  lines) instead of the JSON object
Accuracy (argmax) and cross-entropy against the consensus targets, paired against "text" with a bootstrap 95% interval.
"""
import argparse
import json
from pathlib import Path
import random
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'research/needle-ab'))
from typelogic.offline import install  # noqa: E402
from typelogic import evidence, modes, ollama  # noqa: E402
from typelogic.profiles import active_model_dir  # noqa: E402
from typelogic.server import Application  # noqa: E402
from run_ab import summarize  # noqa: E402  (same metrics as the extracted-fields test)

HERE = Path(__file__).resolve().parent
VARIANTS = ('text', 'case', 'case_oracle', 'case_extracted', 'oracle_only', 'case_text', 'case_text_oracle', 'case_text_extracted')


def same(a, b):
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-6
    return str(a).strip().casefold() == str(b).strip().casefold()


def score_extraction(cases, extracted):
    t = {'fields': 0, 'oracle_values': 0, 'correct': 0, 'wrong': 0, 'wrong_flagged': 0, 'omitted': 0, 'invented': 0, 'seconds': 0.0, 'errors': 0}
    for c in cases:
        e = extracted.get(c['id'], {})
        if 'error' in e:
            t['errors'] += 1
        t['seconds'] += e.get('seconds', 0)
        got = {f['name']: f for f in e.get('fields', [])}
        for f in c['fields']:
            t['fields'] += 1
            want, item = c['oracle'].get(f['name']), got.get(f['name'], {'value': None, 'check': 'not_stated'})
            if want is None:
                t['invented'] += item['value'] is not None
                continue
            t['oracle_values'] += 1
            if item['value'] is None:
                t['omitted'] += 1
            elif same(item['value'], want):
                t['correct'] += 1
            else:
                t['wrong'] += 1
                t['wrong_flagged'] += item['check'] in ('not_in_text', 'inferred')
    t['seconds_per_document'] = round(t['seconds'] / max(1, len(cases)), 2)
    return t


def extract_all(cases, model):
    cache = HERE / f'extraction-{model.replace(":", "_").replace("/", "_")}.json'
    out = json.loads(cache.read_text(encoding='utf-8')) if cache.is_file() else {}
    for c in cases:
        if c['id'] in out and 'error' not in out[c['id']]:
            continue
        try:
            r = evidence.extract(c['state'], c['fields'], 'ollama', model)
            out[c['id']] = {'fields': r['fields'], 'seconds': r['seconds'], 'num_ctx': r.get('num_ctx')}
        except Exception as exc:  # noqa: BLE001 - recorded per document
            out[c['id']] = {'error': str(exc)[:300], 'fields': []}
        print(f'extract {model}: {c["id"]}', file=sys.stderr, flush=True)
    cache.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    return out


def payload(c, variant, extracted):
    base = {'title': 'format A/B', 'threshold': 0.85, 'permutation_check': False, 'use_memory': False, 'exact_checks': []}
    if variant == 'text':
        return {**base, 'state': c['state'], 'questions': c['questions']}
    decisions = []
    for qid, q in c['questions'].items():
        d = {'name': qid, 'output': {'choice': 'choice', 'noul': 'boolean', 'score': 'score'}[q['type']], 'instructions': q['instructions']}
        if q['type'] == 'choice':
            d['alternatives'] = q['criteria']
            if q.get('abstain_option'):
                d['abstain_option'] = q['abstain_option']
        elif q['type'] == 'score':
            d['levels'] = q['criteria']
        elif q.get('criteria'):
            d['criteria'] = q['criteria']
        decisions.append(d)
    schema = [{'name': f['name'], 'type': f['type'], 'description': f.get('description', '')} for f in c['fields']]
    values = {}
    style = 'text' if variant.startswith('case_text') else 'json'
    variant = variant.replace('case_text', 'case')
    if variant in ('case_oracle', 'oracle_only'):
        values = {k: v for k, v in c['oracle'].items() if v is not None}
    elif variant == 'case_extracted':
        values = {f['name']: f['value'] for f in extracted.get(c['id'], {}).get('fields', []) if f['value'] is not None}
    case = {'summary': c['domain'][:1000]}
    if variant != 'oracle_only':
        case['details'] = c['state']
    return {**base, 'case': case, 'evidence_schema': schema, 'evidence': values, 'decisions': decisions, 'engine_input': style}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--cases', type=Path, default=ROOT / 'research/needle-ab/cases.json')
    p.add_argument('--models', default='qwen3.5:4b,qwen3.5:9b')
    p.add_argument('--engines', default='generalist,specialist')
    p.add_argument('--out', type=Path, default=HERE / 'results.json')
    p.add_argument('--variants', default=','.join(VARIANTS), help='Subset of ' + ', '.join(VARIANTS) + ' (text is always included)')
    args = p.parse_args()
    install()
    data = json.loads(args.cases.read_text(encoding='utf-8'))
    cases = data['cases']
    settings = modes.load_settings(Path.home() / '.typelogic-ai')
    print('Ollama:', ollama.ensure_running(settings['ollama_model'], models_dir=settings['ollama_models']), file=sys.stderr, flush=True)

    extractors = {}
    for model in [m for m in args.models.split(',') if m]:
        extracted = extract_all(cases, model)
        extractors[model] = {'scores': score_extraction(cases, extracted), 'values': extracted}
        print(model, json.dumps(extractors[model]['scores']), file=sys.stderr, flush=True)
    best = max(extractors, key=lambda m: extractors[m]['scores']['correct']) if extractors else None
    ollama.unload_all()

    variants = ['text'] + [v for v in args.variants.split(',') if v in VARIANTS and v != 'text']
    rng = random.Random(11)
    rows, timing = [], {}
    for name in [e for e in args.engines.split(',') if e]:
        if name == 'generalist':
            engine = modes.build('hybrid', active_model_dir(ROOT), settings).generalist
        else:
            ollama.unload_all()          # the specialist on the GPU needs the memory the Ollama model holds
            time.sleep(3)
            engine = modes.build('gpu', active_model_dir(ROOT), settings)
        engine.load()
        folder = Path(tempfile.mkdtemp(prefix='case-ab-'))
        app = Application(ROOT, folder, engine, mode='custom')
        t0 = time.perf_counter()
        for c in cases:
            for variant in variants:
                try:
                    response = app.evaluate(payload(c, variant, extractors[best]['values'] if best else {}))
                    for qid, a in response['answers'].items():
                        rows.append({'engine': name, 'variant': variant, 'doc': c['id'], 'qid': qid, 'type': a['type'], 'language': c['language'],
                                     'target': c['targets'][qid], 'probabilities': a['probabilities']})
                except Exception as exc:  # noqa: BLE001
                    rows.append({'engine': name, 'variant': variant, 'doc': c['id'], 'qid': '*', 'error': str(exc)[:300]})
            print(f'{name}: {c["id"]} done', file=sys.stderr, flush=True)
        timing[name] = round(time.perf_counter() - t0, 1)
        app.close()
        shutil.rmtree(folder, ignore_errors=True)
        if name == 'specialist':
            engine.model = None

    def summary(subset):
        return summarize(subset, rng, tuple(variants), 'text')
    engines = sorted({r['engine'] for r in rows})
    report = {'source': data['source'], 'documents': len(cases), 'questions': data['questions'],
              'extractors': {m: v['scores'] for m, v in extractors.items()}, 'best_extractor': best,
              'needle_reference': json.loads((ROOT / 'research/needle-ab/results-specialist.json').read_text(encoding='utf-8'))['needle']
              if (ROOT / 'research/needle-ab/results-specialist.json').is_file() else None,
              'timing_seconds': timing, 'errors': [r for r in rows if 'error' in r][:20],
              'results': {e: summary([r for r in rows if r['engine'] == e]) for e in engines}}
    args.out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    args.out.with_name(args.out.stem + '-rows.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    for e in engines:
        for v, item in report['results'][e].items():
            print(e, v, json.dumps(item), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
