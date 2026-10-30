"""Phase 4 check: the release archive on a clean folder of this computer (Windows).

    python release_verify.py --v1 <TypeLogic-AI> [--online]

1. release/TypeLogic-<version>-windows-x64.zip (tools/package.py) is extracted into spike/work/release-test.
2. `TypeLogic.exe doctor` there: the bundled runtime with the patched llama.dll, no model yet.
3. Started without a console that can answer, TypeLogic.exe does not download anything and says how to run setup.
4. `TypeLogic.exe setup --from <models>`: the models from the local folders (hash checked); Needle 3 is downloaded
   from Hugging Face (weights, license and the pinned wheel, engine taken out with tar.exe).
5. The installed copy: default models, a decision, the 231 JevBench items through the Evaluate page (the Decision
   Index must equal phase 3), Needle extraction; no llama-server left once it stops.
With --online (after the release is published): the specialist GGUF and llama.dll are downloaded from their
models.lock.json URLs and checked.
Writes results/release-verify.json.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
V2 = HERE.parent
RESULTS = HERE / 'results'
TEST = HERE / 'work' / 'release-test'
PORT = 18968
URL = f'http://127.0.0.1:{PORT}'


def say(text):
    print(time.strftime('%H:%M:%S ') + text, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


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


def servers_left():
    cmd = ['powershell', '-NoProfile', '-Command',
           f"@(Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -like '{TEST}*' }}).Count"]
    return int(subprocess.run(cmd, capture_output=True, text=True).stdout.strip() or 0)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--v1', type=Path, required=True)
    p.add_argument('--online', action='store_true', help='also download the published release assets')
    args = p.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    checks, record = [], {}

    def check(name, ok, detail):
        checks.append([name, bool(ok), str(detail)[:500]])
        say(f"{'PASS' if ok else 'FAIL'} {name}: {str(detail)[:300]}")

    def save():
        record['checks'] = checks
        (RESULTS / 'release-verify.json').write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')

    lock = json.loads((V2 / 'config' / 'models.lock.json').read_text(encoding='utf-8'))
    archives = sorted((V2 / 'release').glob('TypeLogic-*-windows-x64.zip'))
    if not archives:
        raise SystemExit('no release archive; run tools/package.py')
    archive = archives[-1]
    if TEST.exists():
        shutil.rmtree(TEST)
    TEST.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        z.extractall(TEST)
    root = TEST / 'TypeLogic'
    exe = root / 'TypeLogic.exe'
    executables = [n for n in names if n.lower().endswith(('.exe', '.bat', '.cmd', '.ps1'))]
    record['archive'] = {'name': archive.name, 'mib': round(archive.stat().st_size / 2**20, 1), 'files': len(names)}
    check('archive layout', all(n.startswith('TypeLogic/') for n in names) and set(executables) <= {'TypeLogic/TypeLogic.exe', 'TypeLogic/runtime/llama/llama-server.exe'}
          and not any(n.startswith(('TypeLogic/data', 'TypeLogic/models', 'TypeLogic/logs')) for n in names),
          f"{archive.name}: {len(names)} files, {record['archive']['mib']} MiB, executables {executables}")

    env = {k: v for k, v in os.environ.items() if not k.startswith('TYPELOGIC_')}
    doctor = subprocess.run([str(exe), 'doctor'], cwd=root, env=env, capture_output=True, text=True).stdout
    record['doctor_fresh'] = doctor
    check('fresh copy: bundled runtime, no model', 'patched llama.dll' in doctor and 'not installed' in doctor, doctor.splitlines()[2:4])

    # started from a script (stdin not a console): nothing is downloaded
    proc = subprocess.Popen([str(exe), '--no-browser', '--port', str(PORT)], cwd=root, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(6)
    proc.kill()
    out = proc.communicate()[0]
    check('no download without a console to ask', 'run: TypeLogic setup' in out and not (root / 'models').exists(), out.strip().splitlines()[:2])
    save()

    froms = ['--from', str(V2 / 'models'), '--from', str(V2 / 'dist' / 'models')]
    began = time.monotonic()
    setup = subprocess.run([str(exe), 'setup', '--with-clef'] + froms, cwd=root, env=env, capture_output=True, text=True)
    record['setup'] = setup.stdout + setup.stderr
    check('setup on the fresh copy', setup.returncode == 0 and 'Setup complete' in setup.stdout, f"{time.monotonic() - began:.0f} s: " + ' | '.join(l for l in setup.stdout.splitlines() if l.strip())[:400])
    # Needle from Hugging Face: a second setup into a scratch folder without --from for it
    needle_dir = root / 'models' / 'needle'
    if needle_dir.exists():
        shutil.rmtree(needle_dir)
    began = time.monotonic()
    setup = subprocess.run([str(exe), 'setup'] + froms[:2], cwd=root, env=env, capture_output=True, text=True)
    record['setup_needle_download'] = setup.stdout + setup.stderr
    lib = needle_dir / 'libneedle3.dll'
    ok = lib.is_file() and sha256(lib) == lock['needle']['platforms']['win_amd64']['sha256']
    check('Needle 3 downloaded and taken out of the wheel', setup.returncode == 0 and ok,
          f"{time.monotonic() - began:.0f} s: " + ' | '.join(l for l in setup.stdout.splitlines() if 'needle' in l.lower())[:400])
    save()

    log = open(RESULTS / 'release-server.log', 'w', encoding='utf-8')
    proc = subprocess.Popen([str(exe), '--no-browser', '--port', str(PORT)], cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    try:
        began = time.monotonic()
        h = {}
        while time.monotonic() - began < 1200:
            try:
                _, h = call('/api/health', timeout=5)
                if h.get('inference') in ('ready', 'error'):
                    break
            except OSError:
                pass
            time.sleep(2)
        check('installed copy starts', h.get('inference') == 'ready', f"{time.monotonic() - began:.0f} s, {' + '.join(h.get('engines') or [])} ({h.get('plan')})")
        html = urllib.request.urlopen(URL + '/', timeout=10).read().decode('utf-8')
        token = re.search(r'name="typelogic-token" content="([^"]+)"', html).group(1)
        version = re.search(r'TypeLogic AI <span>([^ <]+)', html)
        record['version'] = version.group(1) if version else None
        status, r = call('/api/evaluate', {'case': 'The customer was charged twice for order 1182 and asks for the duplicate charge back.',
                                           'decisions': [{'output': 'boolean', 'instructions': 'Should the duplicate charge be refunded?'}]}, token)
        answer = next(iter(r.get('answers', {}).values()), {})
        check('decision', status == 200 and answer.get('engine'), f"{status}, {answer.get('engine')}, {r.get('title')!r}")
        status, job = call('/api/eval/start', {'source': 'jevbench', 'engine': 'auto', 'combine': 'route', 'probes': True}, token)
        while status == 202:
            _, job = call('/api/eval', token=token)
            if job.get('status') != 'running':
                break
            time.sleep(5)
        d = ((job.get('summary') or {}).get('decision_index') or {}).get('value')
        ref = ((json.loads((RESULTS / 'phase3.json').read_text(encoding='utf-8')).get('evaluations') or {}).get('route') or {}).get('decision_index') \
            if (RESULTS / 'phase3.json').is_file() else None
        check('Decision Index as in phase 3', d is not None and ref is not None and abs(d - ref) < 0.5,
              f"{d:.1f} vs {ref:.1f} (phase 3)" if d is not None and ref is not None else job)
        _, n = call('/api/needle', token=token)
        status, ex = call('/api/extract', {'text': 'Invoice INV-2207 from Northwind Traders, due on 15 March 2026.',
                                           'fields': [{'name': 'document_number', 'type': 'text'}, {'name': 'due_date', 'type': 'date'}]}, token)
        got = {f['name']: f['value'] for f in ex.get('fields', [])}
        check('Needle on the installed copy', n.get('available') and status == 200 and got.get('document_number') == 'INV-2207', f"{status}, {got}")
    finally:
        proc.terminate()
        proc.wait(60)
        for _ in range(30):
            if servers_left() == 0:
                break
            time.sleep(1)
        check('no llama-server left', servers_left() == 0, 'after the stop')
        save()

    if args.online:
        for key, entry in (('specialist', lock['built']['spec-q8']), ('llama.dll', lock['runtime']['llama-dll-patched'])):
            dest = TEST / ('download-' + entry['file'])
            began = time.monotonic()
            rc = subprocess.run(['curl.exe', '-L', '--fail', '-s', '-o', str(dest), entry['url']]).returncode
            ok = rc == 0 and dest.is_file() and sha256(dest) == entry['sha256']
            check(f'published {key} downloads and matches', ok, f"{entry['url']} ({time.monotonic() - began:.0f} s, curl {rc})")
            if dest.exists():
                dest.unlink()
        save()
    say('release checks: ' + ', '.join(f"{n}: {'PASS' if ok else 'FAIL'}" for n, ok, _ in checks))


if __name__ == '__main__':
    main()
