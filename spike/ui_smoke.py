"""Phase 2 check with the real models: the UI API of TypeLogic.exe (dist/, installed by app_verify.py).

    python ui_smoke.py --v1 <TypeLogic-AI>

Starts dist/TypeLogic.exe with its default models, then through the session-authenticated UI API: a template case
(title written by Plumb, evidence, three decisions), evidence extraction by Plumb, a 77-option BANKING77 catalog
question, the library views and exports, and the CPU-only mode and back. Writes results/ui-smoke.json.
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
PORT = 18966
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


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--v1', type=Path, required=True)
    args = p.parse_args()
    checks, record = [], {}

    def check(name, ok, detail):
        checks.append([name, bool(ok), str(detail)[:400]])
        say(f"{'PASS' if ok else 'FAIL'} {name}: {str(detail)[:200]}")

    env = {k: v for k, v in os.environ.items() if not k.startswith('TYPELOGIC_')}
    log = open(RESULTS / 'ui-server.log', 'w', encoding='utf-8')
    proc = subprocess.Popen([str(DIST / f'TypeLogic{EXE}'), 'serve', '--no-browser', '--port', str(PORT)], cwd=DIST, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        h = wait_ready(proc)
        record['health'] = h
        html = urllib.request.urlopen(URL + '/', timeout=10).read().decode('utf-8')
        token = re.search(r'name="typelogic-token" content="([^"]+)"', html).group(1)
        check('page and session token', len(token) >= 32 and '2.0.0' in html, f'{len(html)} bytes')
        status, _ = call('/api/status')
        check('API refuses a request without the token', status == 401, status)

        _, rows = call('/api/templates', token=token)
        templates = {r['id']: r['template'] for r in rows['templates']}
        t = templates['billing-duplicate-payment']
        request = {'template': t['id'], 'case': t['case'], 'evidence_schema': t.get('evidence_schema', []), 'decisions': t['decisions'],
                   'threshold': t.get('threshold', 0.85), 'permutation_check': True, 'tags': t.get('tags', []), 'space': 'default',
                   'evidence': {k: {'value': v, 'source': 'template', 'status': 'confirmed'} for k, v in (t.get('evidence') or {}).items()}}
        began = time.perf_counter()
        status, r = call('/api/evaluate', request, token)
        seconds = time.perf_counter() - began
        record['template_case'] = r
        engines = sorted({a.get('engine') for a in r.get('answers', {}).values()})
        check('template case evaluated', status == 200 and len(r.get('answers', {})) == len(t['decisions']), f'{status}, {seconds:.1f} s, engines {engines}')
        check('title written by Plumb', str(r.get('title_source', '')).startswith('model:'), f"{r.get('title')!r} ({r.get('title_source')})")
        check('stored with verified integrity', (r.get('integrity') or {}).get('verified') is True, r.get('decision_id'))

        tr = templates['ticket-routing']
        text = 'My payout has failed three days in a row and support chat keeps timing out. I need this fixed today.'
        began = time.perf_counter()
        status, ex = call('/api/extract-evidence', {'text': text, 'schema': tr['evidence_schema'], 'backend': 'auto'}, token)
        record['extraction'] = ex
        fields = {f['name']: f for f in ex.get('fields', [])}
        days = fields.get('payout_failed_days', {})
        check('evidence extraction by Plumb', status == 200 and days.get('value') == 3 and days.get('check') == 'in_text',
              f"{status}, {time.perf_counter() - began:.1f} s, {[(f['name'], f['value'], f['check']) for f in ex.get('fields', [])]}")

        item = suites.load(args.v1, 'banking77', 1)[0]
        status, r = call('/api/evaluate', {**suites.payload(item), 'permutation_check': False, 'tags': ['banking77']}, token)
        answer = next(iter(r.get('answers', {}).values()), {})
        n = len(answer.get('probabilities', {}))
        check('77-option catalog routed to a decision model', status == 200 and n == 77 and answer.get('engine') in ('clef', 'specialist'),
              f"{status}, engine {answer.get('engine')}, choice {answer.get('choice')} (gold {item['gold']['intent']})")

        _, lib = call('/api/decisions?space=default', token=token)
        check('library lists the decisions', len(lib.get('decisions', [])) >= 2, len(lib.get('decisions', [])))
        did = lib['decisions'][0]['id']
        status, lite = call(f'/api/decision?id={did}&format=lite', token=token)
        check('Lite export', status == 200 and list(lite) == ['title', 'tags', 'questions'], list(lite))
        status, exp = call('/api/export', token=token)
        check('full export', status == 200 and len(exp.get('decisions', [])) >= 2, len(exp.get('decisions', [])))

        status, m = call('/api/mode', {'mode': 'cpu'}, token)
        began = time.perf_counter()
        while time.perf_counter() - began < 900:
            _, s = call('/api/status', token=token)
            if s['engine']['status'] in ('ready', 'error'):
                break
            time.sleep(2)
        check('CPU only mode loads', s['engine']['status'] == 'ready' and s['engine'].get('device') == 'CPU',
              f"{time.perf_counter() - began:.0f} s, {s['engine'].get('plan')}")
        began = time.perf_counter()
        status, r = call('/api/evaluate', {'case': 'The customer was charged twice for order 1182 and asks for the duplicate charge back.',
                                           'decisions': [{'output': 'boolean', 'instructions': 'Should the duplicate charge be refunded?'}]}, token)
        check('decision on the CPU', status == 200, f"{status}, {time.perf_counter() - began:.1f} s, {next(iter(r.get('answers', {}).values()), {}).get('engine')}")
        call('/api/mode', {'mode': 'auto'}, token)
        began = time.perf_counter()
        while time.perf_counter() - began < 900:
            _, s = call('/api/status', token=token)
            if s['engine']['status'] in ('ready', 'error'):
                break
            time.sleep(2)
        check('back to Automatic', s['engine']['status'] == 'ready' and s['engine'].get('device') != 'CPU', s['engine'].get('plan'))
    finally:
        proc.kill()
        proc.wait(60)
        record['checks'] = checks
        (RESULTS / 'ui-smoke.json').write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')
    say('ui checks: ' + ', '.join(f"{n}: {'PASS' if ok else 'FAIL'}" for n, ok, _ in checks))


if __name__ == '__main__':
    main()
