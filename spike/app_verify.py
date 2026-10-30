"""Phase 1 acceptance: build TypeLogic.exe, install it with `setup` into dist/, and check it against the spike.

    python app_verify.py --v1 <TypeLogic-AI> [--banking 1000] [--limit N]

1. `cargo build --release`, then dist/ = TypeLogic.exe + config/models.lock.json, and
   `TypeLogic setup --with-clef --from models --from runtime/llama-fix --from runtime/downloads` (hard links, hashes checked).
2. Default start: the models chosen for this computer (/api/health), the suites ≤ 16 options through automatic routing
   (results/app-auto), a non-loopback connection refused, and no llama-server left once TypeLogic.exe stops.
3. The three models (TYPELOGIC_MODELS=specialist,generalist,clef), each engine forced on its suites
   (results/app-generalist, app-specialist, app-clef); then TypeLogic.exe is killed: its llama-server processes must go too.
Writes results/app-verify.json; report.py compares the answers question by question with the trio runs.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
V2 = HERE.parent
RESULTS = HERE / 'results'
EXE = '.exe' if os.name == 'nt' else ''
DIST = V2 / 'dist'
PORT = 18965


def say(text):
    print(time.strftime('%H:%M:%S ') + text, flush=True)


def run(args, **kw):
    say('$ ' + ' '.join(str(a) for a in args))
    proc = subprocess.run([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', **kw)
    sys.stdout.write(proc.stdout)
    if proc.returncode:
        raise SystemExit(f'command failed ({proc.returncode}): {Path(str(args[0])).name}')
    return proc.stdout


def cargo():
    found = shutil.which('cargo') or str(Path.home() / '.cargo' / 'bin' / f'cargo{EXE}')
    if not Path(found).is_file():
        raise SystemExit('cargo not found: install Rust (rustup) or add it to PATH')
    return found


def health(timeout=3):
    with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/health', timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def start(env_extra):
    log = open(RESULTS / 'app-server.log', 'a', encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if not k.startswith('TYPELOGIC_')}
    env.update(env_extra)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    proc = subprocess.Popen([str(DIST / f'TypeLogic{EXE}'), 'serve', '--no-browser', '--port', str(PORT)], cwd=DIST, env=env,
                            stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    began = time.monotonic()
    while time.monotonic() - began < 1200:
        if proc.poll() is not None:
            raise SystemExit(f'TypeLogic.exe exited with {proc.returncode}; see results/app-server.log')
        try:
            h = health()
            if h.get('inference') == 'ready':
                say(f'TypeLogic ready in {time.monotonic() - began:.0f} s: {h.get("engines")} ({h.get("plan")})')
                return proc, h
        except OSError:
            pass
        time.sleep(2)
    proc.kill()
    raise SystemExit('TypeLogic.exe did not become ready')


def servers_left():
    """llama-server processes started from dist/ that are still running."""
    if os.name == 'nt':
        cmd = ['powershell', '-NoProfile', '-Command',
               f"@(Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -like '{DIST}*' }}).Count"]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
        return int(out or 0)
    out = subprocess.run(['pgrep', '-f', str(DIST / 'runtime')], capture_output=True, text=True).stdout.split()
    return len(out)


def gone_after(proc, kill):
    if kill:
        proc.kill()              # TerminateProcess: no clean-up code runs, only the job object can end the children
    else:
        proc.terminate()
    proc.wait(60)
    for _ in range(30):
        if servers_left() == 0:
            return True
        time.sleep(1)
    return False


def remote_refused():
    """The API must not answer on a non-loopback address of this computer."""
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)} - {'127.0.0.1'}
    except OSError:
        addrs = set()
    if not addrs:
        return True, 'no non-loopback IPv4 address to test'
    for ip in addrs:
        try:
            with socket.create_connection((ip, PORT), timeout=2):
                return False, f'connection accepted on {ip}:{PORT}'
        except OSError:
            pass
    return True, f'refused on {", ".join(sorted(addrs))}'


def via_app(args, engine, out, names):
    run([sys.executable, HERE / 'via_app.py', '--v1', args.v1, '--url', f'http://127.0.0.1:{PORT}', '--engine', engine,
         '--out', RESULTS / out, '--suites', names, '--banking', str(args.banking)] + (['--limit', str(args.limit)] if args.limit else []))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--v1', type=Path, required=True)
    p.add_argument('--banking', type=int, default=1000)
    p.add_argument('--limit', type=int)
    p.add_argument('--skip-build', action='store_true')
    p.add_argument('--only', choices=('default', 'three', 'setup'), help='run one stage (the other checks of app-verify.json are kept)')
    args = p.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    old = json.loads((RESULTS / 'app-verify.json').read_text(encoding='utf-8')) if args.only and (RESULTS / 'app-verify.json').is_file() else {}
    record = old
    checks = [c for c in old.get('checks', []) if not c[0].startswith(('three models', 'inference server ends with TypeLogic (killed)'))] if args.only == 'three' else \
             [c for c in old.get('checks', []) if c[0].startswith(('three models', 'inference server ends with TypeLogic (killed)'))] if args.only == 'default' else []
    if not args.skip_build:
        run([cargo(), 'build', '--release'], cwd=V2)
    (DIST / 'config').mkdir(parents=True, exist_ok=True)
    shutil.copy2(V2 / 'target' / 'release' / f'TypeLogic{EXE}', DIST / f'TypeLogic{EXE}')
    shutil.copy2(V2 / 'config' / 'models.lock.json', DIST / 'config' / 'models.lock.json')
    run([DIST / f'TypeLogic{EXE}', 'setup', '--with-clef', '--from', V2 / 'models', '--from', V2 / 'runtime' / 'llama-fix',
         '--from', V2 / 'runtime' / 'downloads', '--from', args.v1 / 'models'], cwd=DIST)   # 1.x models/needle: Needle 3
    record['doctor'] = run([DIST / f'TypeLogic{EXE}', 'doctor'], cwd=DIST)
    if args.only == 'setup':
        return

    def save():
        record['checks'] = checks
        (RESULTS / 'app-verify.json').write_text(json.dumps(record, indent=2), encoding='utf-8')

    def stage(name, body):
        """Run one stage; a failure is recorded as a failed check and the next stage still runs."""
        try:
            body()
        except (Exception, SystemExit) as exc:
            say(f'{name} failed: {exc}')
            checks.append([name, False, f'{type(exc).__name__}: {str(exc)[:300]}'])
        save()

    # 2. default start
    def default():
        proc, h = start({})
        try:
            record['default'] = {'engines': h.get('engines'), 'plan': h.get('plan')}
            checks.append(['default start', True, f"{' + '.join(h.get('engines') or [])} ({h.get('plan')})"])
            ok, detail = remote_refused()
            checks.append(['API on loopback only', ok, detail])
            save()
            via_app(args, 'auto', 'app-auto', 'jevbench,test2')
        finally:
            checks.append(['inference server ends with TypeLogic (stopped)', gone_after(proc, kill=False), 'no llama-server left after 30 s'])

    # 3. the three models, each engine forced
    def three():
        proc, h = start({'TYPELOGIC_MODELS': 'specialist,generalist,clef'})
        try:
            record['three'] = {'engines': h.get('engines'), 'plan': h.get('plan')}
            checks.append(['three models start', h.get('engines') == ['specialist', 'generalist', 'clef'], ' + '.join(h.get('engines') or [])])
            save()
            via_app(args, 'generalist', 'app-generalist', 'jevbench,test2')
            via_app(args, 'specialist', 'app-specialist', 'jevbench,test2,banking77')
            via_app(args, 'clef', 'app-clef', 'jevbench,test2,banking77')
        finally:
            checks.append(['inference server ends with TypeLogic (killed)', gone_after(proc, kill=True), 'no llama-server left after 30 s'])

    if not args.only or args.only == 'default':
        stage('default start', default)
    if not args.only or args.only == 'three':
        stage('three models', three)
    say('app checks: ' + ', '.join(f"{n}: {'PASS' if ok else 'FAIL'}" for n, ok, _ in checks))


if __name__ == '__main__':
    main()
