"""Phase 3 check with the real models: combined mode, Evaluate page, bias probes and Needle 3 (dist/, installed by
app_verify.py --only setup).

    python phase3_verify.py --v1 <TypeLogic-AI>

1. dist/TypeLogic.exe with its default models; /v1/decide?combine=ensemble on JevBench and test2 -> results/app-ensemble
   (report.py compares it with the product of experts computed from the trio runs measured alone).
2. The Evaluate page API on the 231 public JevBench items: automatic routing with the order probe, combined mode,
   Clef-Flash only; the routed accuracy must equal the one of results/app-auto (same answers, other code path).
3. Needle 3 through its native engine: status, /api/extract, evidence extraction with backend needle, and a decision
   carrying extracted_fields (every answer flagged).
Writes results/phase3.json.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
V2 = HERE.parent
DIST = V2 / 'dist'
RESULTS = HERE / 'results'
EXE = '.exe' if os.name == 'nt' else ''
PORT = 18967
URL = f'http://127.0.0.1:{PORT}'
sys.path.insert(0, str(HERE))
import suites  # noqa: E402


def say(text):
    print(time.strftime('%H:%M:%S ') + text, flush=True)


def call(path, payload=None, token=None, timeout=1800):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-TypeLogic-Token'] = token
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(URL + path, data=data, headers=headers, method='POST' if payload is not None else 'GET')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode('utf-8', 'replace') or '{}')


def wait_ready(proc, timeout=1200):
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        if proc.poll() is not None:
            raise SystemExit(f'TypeLogic.exe exited with {proc.returncode}')
        try:
            status, h = call('/api/health', timeout=5)
            if status == 200 and h.get('inference') == 'ready':
                return h
            if h.get('inference') == 'error':
                raise SystemExit('the models did not load')
        except OSError:
            pass
        time.sleep(2)
    raise SystemExit('TypeLogic.exe did not become ready')


def accuracy_of(folder, name, items):
    rows = {}
    path = folder / f'{name}.jsonl'
    if not path.is_file():
        return None
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r['id']] = r
    n = k = 0
    for item in items:
        for qid, q in item['questions'].items():
            keys = suites.canonical_keys(q)
            n += 1
            d = rows.get(item['id'], {}).get('dist', {}).get(qid)
            k += bool(d) and keys[max(range(len(keys)), key=d.__getitem__)] == item['gold'][qid]
    return k / n if n else None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--v1', type=Path, required=True)
    p.add_argument('--skip-suites', action='store_true', help='skip step 1 (results/app-ensemble already measured)')
    p.add_argument('--only', choices=('needle',), help='needle: only step 3 (the other checks of phase3.json are kept)')
    args = p.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    checks, record = [], {}
    if args.only and (RESULTS / 'phase3.json').is_file():
        record = json.loads((RESULTS / 'phase3.json').read_text(encoding='utf-8'))
        checks = [c for c in record.get('checks', []) if not any(w in c[0] for w in ('Needle', 'needle', 'extracted fields', 'default start'))]

    def check(name, ok, detail):
        checks.append([name, bool(ok), str(detail)[:500]])
        say(f"{'PASS' if ok else 'FAIL'} {name}: {str(detail)[:300]}")

    def save():
        record['checks'] = checks
        (RESULTS / 'phase3.json').write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')

    env = {k: v for k, v in os.environ.items() if not k.startswith('TYPELOGIC_')}
    log = open(RESULTS / 'phase3-server.log', 'w', encoding='utf-8')
    proc = subprocess.Popen([str(DIST / f'TypeLogic{EXE}'), 'serve', '--no-browser', '--port', str(PORT)], cwd=DIST, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        h = wait_ready(proc)
        record['health'] = h
        check('default start', 'generalist' in (h.get('engines') or []), f"{' + '.join(h.get('engines') or [])} ({h.get('plan')})")
        html = urllib.request.urlopen(URL + '/', timeout=10).read().decode('utf-8')
        token = re.search(r'name="typelogic-token" content="([^"]+)"', html).group(1)

        # 1. combined mode through the machine API
        if args.only == 'needle':
            pass
        elif not args.skip_suites:
            subprocess.run([sys.executable, str(HERE / 'via_app.py'), '--v1', str(args.v1), '--url', URL, '--combine', 'ensemble',
                            '--out', str(RESULTS / 'app-ensemble'), '--suites', 'jevbench,test2'], check=True)
        rows = [] if args.only else [json.loads(s) for s in (RESULTS / 'app-ensemble' / 'jevbench.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
        engines = {}
        for r in rows:
            for e in (r.get('engines') or {}).values():
                engines[e] = engines.get(e, 0) + 1
        if not args.only:
            check('combined answers carry engine ensemble', engines.get('ensemble', 0) >= 0.9 * len(rows) and not any('error' in r for r in rows), engines)
        save()

        # 2. Evaluate page
        jev = suites.load(args.v1, 'jevbench')
        routed_app = accuracy_of(RESULTS / 'app-auto', 'jevbench', jev)
        runs = {}
        for name, body in [] if args.only else [('route', {'engine': 'auto', 'combine': 'route', 'probes': True}),
                           ('ensemble', {'engine': 'auto', 'combine': 'ensemble', 'probes': False}),
                           ('clef', {'engine': 'clef', 'combine': 'route', 'probes': True})]:
            if name == 'clef' and 'clef' not in (h.get('engines') or []):
                continue
            status, job = call('/api/eval/start', {'source': 'jevbench', **body}, token)
            if status != 202:
                check(f'evaluation {name} starts', False, job)
                continue
            began = time.monotonic()
            while True:
                _, job = call('/api/eval', token=token)
                if job.get('status') != 'running' or time.monotonic() - began > 3600:
                    break
                time.sleep(5)
            s = job.get('summary') or {}
            o, d, pr = s.get('overall', {}), s.get('decision_index', {}), s.get('probes', {})
            runs[name] = {'seconds': round(time.monotonic() - began), 'decision_index': d.get('value'), 'speed': d.get('speed'),
                          'accuracy': o.get('accuracy'), 'brier': o.get('brier'), 'ece': o.get('ece_10'), 'failed': o.get('questions', 0) - o.get('valid', 0),
                          'by_tier': {k: v.get('accuracy') for k, v in (s.get('by_tier') or {}).items()}, 'probes': pr, 'saved': job.get('saved')}
            check(f'evaluation {name}', job.get('status') == 'done' and o.get('questions') == 231 and job.get('saved'),
                  f"index {d.get('value'):.1f}, accuracy {o.get('accuracy'):.3f}, Brier {o.get('brier'):.3f}, {runs[name]['seconds']} s, failed {runs[name]['failed']}"
                  if d.get('value') is not None else job)
            save()
        if runs:
            record['evaluations'] = runs
        if 'route' in runs and routed_app is not None:
            # same answers through another code path; GPU arithmetic may move one near-tie (phase 1: 1 of 846)
            check('Evaluate page = /v1/decide (automatic routing)', abs(runs['route']['accuracy'] - routed_app) * 231 <= 1.01,
                  f"{runs['route']['accuracy']:.4f} vs results/app-auto {routed_app:.4f} (at most one answer apart)")
        if 'route' in runs:
            order = runs['route']['probes'].get('order', {})
            check('order probe ran on the Choice questions', order.get('checked', 0) >= 100, order)
        _, saved = call('/api/evaluations', token=token)
        if not args.only:
            check('saved evaluations listed', len(saved.get('evaluations', [])) >= len(runs), len(saved.get('evaluations', [])))

        # 3. Needle 3
        _, n = call('/api/needle', token=token)
        record['needle_status'] = n
        check('Needle installed and verified', n.get('available') is True, n.get('reason') or n.get('folder'))
        text = 'Invoice INV-2207 from Northwind Traders: total 1,250.40 EUR, due on 15 March 2026. Please pay by bank transfer.'
        fields = [{'name': 'vendor', 'type': 'text', 'description': 'Vendor or supplier name'},
                  {'name': 'document_number', 'type': 'text', 'description': 'Invoice or quote number'},
                  {'name': 'amount', 'type': 'number', 'description': 'Total amount'},
                  {'name': 'currency', 'type': 'text', 'description': 'Currency code'},
                  {'name': 'due_date', 'type': 'date', 'description': 'Due date'}]
        status, ex = call('/api/extract', {'text': text, 'fields': fields}, token)
        _, warm = call('/api/extract', {'text': text, 'fields': fields}, token)       # engine loaded, same field list
        record['needle_extract'] = ex
        got = {f['name']: (f['value'], f['check']) for f in ex.get('fields', [])}
        # the engine's own reading is not judged here (a small model; 1.x gives the same, see needle-v1.json):
        # the fields come back typed, checked against the text, and the same on a second call
        stated = [k for k, (v, c) in got.items() if v is not None and c == 'in_text']
        check('Needle /api/extract', status == 200 and len(stated) >= 3 and warm.get('fields') == ex.get('fields'),
              f"{status}, first {ex.get('seconds')} s, warm {warm.get('seconds')} s, {got}")
        v1 = RESULTS / 'needle-v1.json'
        if v1.is_file():
            ref = json.loads(v1.read_text(encoding='utf-8'))
            same = [f['value'] for f in ref['fields']] == [f['value'] for f in ex.get('fields', [])]
            check('Needle fields = TypeLogic 1.x (same engine, same text)', same,
                  f"1.x {[(f['name'], f['value']) for f in ref['fields']]}, warm {ref.get('warm_seconds')} s")
        _, rows = call('/api/templates', token=token)
        tr = {r['id']: r['template'] for r in rows['templates']}['ticket-routing']
        status, ev = call('/api/extract-evidence', {'text': 'My payout has failed three days in a row and support chat keeps timing out. I need this fixed today.',
                                                    'schema': tr['evidence_schema'], 'backend': 'needle'}, token)
        record['needle_evidence'] = ev
        check('evidence extraction with backend needle', status == 200 and ev.get('backend') == 'needle',
              f"{status}, {ev.get('seconds')} s, {[(f['name'], f['value'], f['check']) for f in ev.get('fields', [])] if status == 200 else ev}")
        if status == 200 and ex.get('fields'):
            status, r = call('/api/evaluate', {'case': text, 'decisions': [{'output': 'boolean', 'instructions': 'Is the invoice due before April 2026?'}],
                                               'extracted_fields': ex, 'permutation_check': False}, token)
            answer = next(iter(r.get('answers', {}).values()), {})
            check('decision with extracted fields is flagged', status == 200 and 'model_extracted_fields' in answer.get('review_reasons', [])
                  and r.get('extracted_fields', {}).get('values_are_unverified') is True, f"{status}, {answer.get('review_reasons')}")
    finally:
        proc.kill()
        proc.wait(60)
        save()
    say('phase 3 checks: ' + ', '.join(f"{n}: {'PASS' if ok else 'FAIL'}" for n, ok, _ in checks))


if __name__ == '__main__':
    main()
