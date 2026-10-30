"""TypeLogic v2 spikes: does the llama.cpp path keep the specialist's answers, and which generalist earns its place?

Steps (each can be run alone; `all` runs them in order and skips work already done):
    fetch      llama.cpp v0.8.0 Windows Vulkan build + the pinned GGUF files of config/models.lock.json
               (a file already in the local Ollama store with the same SHA-256 is copied instead of downloaded)
    convert    specialist -> GGUF F16 (offline, llama.cpp KevModel converter), then Q8_0 and Q4_K_M with llama-quantize
    reference  PyTorch reference answers with TypeLogic 1.x (XPU)
    bench      every GGUF target in llama-server on the GPU (Vulkan), one at a time
    report     results/REPORT.md and results/summary.json with the gates of docs/SPIKES.md
    app        phase 1 acceptance: build TypeLogic.exe, install it in dist/ and compare it with the spike (app_verify.py)
    ui         phase 2 check: build and install, then the UI API with the real models (ui_smoke.py)

    run-spikes.bat [all|fetch|convert|reference|bench|report] [--banking N] [--with-kev] [--only a,b]

Runs with the Python of TypeLogic 1.x. Only `fetch` uses the network (GitHub and Hugging Face, pinned revisions,
SHA-256 checked); every other step is offline and binds to 127.0.0.1 only.
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
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
V2 = HERE.parent
LOCK = json.loads((V2 / 'config' / 'models.lock.json').read_text(encoding='utf-8'))
RUNTIME = V2 / 'runtime' / 'llama'
RUNTIME_FIX = V2 / 'runtime' / 'llama-fix'      # the release with llama.dll rebuilt from patches/ (step llama-fix)
PATCH = HERE / 'patches' / 'qwen35-skip-lm-head.patch'
MODELS = V2 / 'models'
RESULTS = HERE / 'results'
EXE = '.exe' if os.name == 'nt' else ''
SPEC = {k: MODELS / v['file'] for k, v in LOCK['built'].items()}
TARGETS = ['spec-f16', 'spec-q8', 'spec-q4', 'clef-flash-q4', 'plumb-q8', 'plumb-q4', 'kev4b-q4']


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for s in self.streams:
            s.write(text)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


def say(text):
    print(time.strftime('%H:%M:%S ') + text, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 22), b''):
            h.update(block)
    return h.hexdigest()


def run(args):
    """Run a child process, its output copied to the console and to spike.log."""
    say('$ ' + ' '.join(str(a) for a in args))
    proc = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', errors='replace', env={**os.environ, 'PYTHONUNBUFFERED': '1'})
    for line in proc.stdout:
        sys.stdout.write(line)
    if proc.wait():
        raise SystemExit(f'command failed ({proc.returncode}): {Path(str(args[0])).name} {Path(str(args[1])).name if len(args) > 1 else ""}')


# ------------------------------------------------------------------------------------------------ fetch

class DownloadLock:
    """One downloader per file: an exclusive lock file, released by the operating system if the process dies."""

    def __init__(self, path):
        self.path, self.handle = path, None

    def __enter__(self):
        self.handle = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise SystemExit(f'{self.path.name}: another fetch is already downloading this file; wait for it to finish') from None
        return self

    def __exit__(self, *exc):
        self.handle.close()
        try:
            self.path.unlink()
        except OSError:
            pass


def download(url, dest, size=None, digest=None):
    """Resumable download to dest (.part first), then size and SHA-256 checks before the rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + '.part')
    with DownloadLock(dest.with_name(dest.name + '.lock')):
        have = part.stat().st_size if part.exists() else 0
        if size and have >= size:              # a complete or damaged earlier attempt: check it, else start over
            if have == size and (not digest or sha256(part) == digest):
                part.replace(dest)
                return
            part.unlink()
            have = 0
        request = urllib.request.Request(url, headers={'User-Agent': 'TypeLogic-v2-spike', **({'Range': f'bytes={have}-'} if have else {})})
        with urllib.request.urlopen(request, timeout=60) as response:
            if have and response.status != 206:
                have = 0
            total = int(response.headers.get('Content-Length') or 0) + have
            with part.open('ab' if have else 'wb') as out:
                done, last = have, time.monotonic()
                while True:
                    block = response.read(1 << 22)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if time.monotonic() - last > 10:
                        say(f'  {dest.name}: {done / 2**30:.2f} / {total / 2**30:.2f} GiB')
                        last = time.monotonic()
        got = part.stat().st_size
        if size and got != size:
            part.unlink()
            raise SystemExit(f'{dest.name}: size {got} != {size}; the partial file was removed, run fetch again')
        if digest:
            actual = sha256(part)
            if actual != digest:
                part.unlink()
                raise SystemExit(f'{dest.name}: SHA-256 mismatch ({actual}); the partial file was removed, run fetch again')
        part.replace(dest)


def ollama_blob(digest):
    root = Path(os.environ.get('OLLAMA_MODELS') or Path.home() / '.ollama' / 'models')
    blob = root / 'blobs' / f'sha256-{digest}'
    return blob if blob.is_file() else None


def fetch_runtime():
    info = LOCK['runtime']['llama.cpp']
    if (RUNTIME / f'llama-server{EXE}').is_file() and (RUNTIME / 'llama-runtime.json').is_file():
        say(f'llama.cpp {info["tag"]} already in {RUNTIME}')
        return
    archive = V2 / 'runtime' / 'downloads' / info['asset']
    if not archive.is_file():
        say(f'downloading {info["asset"]} ({info["bytes"] / 2**20:.0f} MiB), llama.cpp {info["tag"]} = build {info["binaries_tag"]}')
        url = f'https://github.com/{info["repo"]}/releases/download/{info["binaries_tag"]}/{info["asset"]}'
        download(url, archive, info['bytes'], info['sha256'])
    elif sha256(archive) != info['sha256']:
        archive.unlink()
        raise SystemExit(f'{archive.name}: SHA-256 mismatch; the file was removed, run fetch again')
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    RUNTIME.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            name = Path(member.filename)
            if member.is_dir() or name.is_absolute() or '..' in name.parts:
                continue
            target = RUNTIME / name.name          # flat layout: the release zip has no sub-folders that matter
            with z.open(member) as src, target.open('wb') as dst:
                shutil.copyfileobj(src, dst)
    if not (RUNTIME / f'llama-server{EXE}').is_file():
        raise SystemExit('llama-server.exe not found in the release archive')
    (RUNTIME / 'llama-runtime.json').write_text(json.dumps({k: info[k] for k in ('tag', 'binaries_tag', 'commit', 'asset', 'sha256')}, indent=2),
                                                encoding='utf-8')
    say(f'llama.cpp {info["tag"]} ready in {RUNTIME}')


def fetch_models(with_kev, with_clef_variants=False):
    for key, m in LOCK['models'].items():
        if m.get('group') == 'clef-variants':
            if not with_clef_variants:
                continue
        elif m.get('optional') and not with_kev:
            continue
        dest = MODELS / m['file']
        if dest.is_file() and dest.stat().st_size == m['bytes']:
            say(f'{key}: present ({m["file"]})')
            continue
        if m.get('v2') == 'spike' and ((RESULTS / key / 'target.json').is_file() or (RESULTS / f'trio-{key}.json').is_file()):
            say(f'{key}: already measured, not needed by v2 (not downloaded again)')
            continue
        blob = ollama_blob(m['sha256'])
        if blob and blob.stat().st_size == m['bytes']:
            say(f'{key}: copying from the local Ollama store')
            MODELS.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(blob, dest)
            if sha256(dest) != m['sha256']:
                dest.unlink()
                raise SystemExit(f'{key}: Ollama copy failed its SHA-256 check')
            continue
        say(f'{key}: downloading {m["file"]} ({m["bytes"] / 2**30:.2f} GiB) from {m["repo"]}@{m["revision"][:12]}')
        download(f'https://huggingface.co/{m["repo"]}/resolve/{m["revision"]}/{m["file"]}', dest, m['bytes'], m['sha256'])


# ------------------------------------------------------------------------------------------------ convert

def convert(args):
    if not SPEC['spec-f16'].is_file():
        run([sys.executable, HERE / 'convert_specialist.py', '--profile', args.profile, '--v1', args.v1, '--out', SPEC['spec-f16'], '--outtype', 'f16'])
    quantize = RUNTIME / f'llama-quantize{EXE}'
    for key, qtype in (('spec-q8', 'Q8_0'), ('spec-q4', 'Q4_K_M')):
        if SPEC[key].is_file():
            continue
        # the pointer head ([q | k], 2 x 256 x 2048) stays in F16: it is tiny and it is the readout itself
        run([quantize, '--tensor-type', 'cls.output=f16', SPEC['spec-f16'], SPEC[key], qtype])
    for key in ('spec-f16', 'spec-q8', 'spec-q4'):
        say(f'{key}: {SPEC[key].name} {SPEC[key].stat().st_size / 2**30:.2f} GiB')


# ------------------------------------------------------------------------------------------------ llama-fix

def visual_studio():
    """(vcvarsall.bat, installation folder) of the newest Visual Studio with the C++ x64 tools, else SystemExit."""
    vswhere = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Microsoft Visual Studio' / 'Installer' / 'vswhere.exe'
    if vswhere.is_file():
        out = subprocess.run([str(vswhere), '-latest', '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
                              '-property', 'installationPath'], capture_output=True, text=True)
        root = Path(out.stdout.strip().splitlines()[0]) if out.stdout.strip() else None
        if root and (root / 'VC' / 'Auxiliary' / 'Build' / 'vcvarsall.bat').is_file():
            return root / 'VC' / 'Auxiliary' / 'Build' / 'vcvarsall.bat', root
    raise SystemExit('Visual Studio C++ build tools not found. Install them with:\n'
                     '  winget install --id Microsoft.VisualStudio.2022.BuildTools -e --override '
                     '"--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"')


def build_tool(name, vs_root):
    """cmake/ninja from PATH or from the copy bundled with Visual Studio."""
    found = shutil.which(name)
    if found:
        return Path(found)
    bundled = vs_root / 'Common7' / 'IDE' / 'CommonExtensions' / 'Microsoft' / 'CMake'
    for candidate in (bundled / 'CMake' / 'bin' / f'{name}.exe', bundled / 'Ninja' / f'{name}.exe'):
        if candidate.is_file():
            return candidate
    return None


def llama_fix(args):
    """Rebuild llama.dll of the pinned commit with patches/qwen35-skip-lm-head.patch (decision models in embedding
    mode skip the LM head) and put it into a copy of the release runtime: runtime/llama-fix. Only llama.dll changes;
    the Vulkan backend and the server are the release binaries. Needs git and the Visual Studio C++ tools."""
    info = LOCK['runtime']['llama.cpp']
    if (RUNTIME_FIX / 'llama-fix.json').is_file() and not args.force:
        say(f'patched runtime already in {RUNTIME_FIX} (use --force to rebuild)')
        return
    if not (RUNTIME / f'llama-server{EXE}').is_file():
        raise SystemExit('run fetch first (the release runtime is copied)')
    git = shutil.which('git')
    if not git:
        raise SystemExit('git not found (winget install --id Git.Git -e)')
    vcvars, vs_root = visual_studio()
    cmake, ninja = build_tool('cmake', vs_root), build_tool('ninja', vs_root)
    if not cmake:
        raise SystemExit('CMake not found. Install it with: winget install --id Kitware.CMake -e   (or the "C++ CMake tools" of Visual Studio)')
    src, build = V2 / 'runtime' / 'src' / 'llama.cpp', V2 / 'runtime' / 'src' / 'build-llama'
    if not (src / '.git').is_dir():
        src.parent.mkdir(parents=True, exist_ok=True)
        # LF line endings, as in the repository: the patch applies byte for byte whatever core.autocrlf says
        run([git, '-c', 'core.autocrlf=false', 'clone', '--depth', '1', '--branch', info['tag'], f'https://github.com/{info["repo"]}', src])
    head = subprocess.run([git, '-C', src, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    if head != info['commit']:
        raise SystemExit(f'llama.cpp source is at {head}, expected {info["commit"]}')
    run([git, '-C', src, 'checkout', '--', '.'])                  # a clean tree, then the patch
    run([git, '-C', src, 'apply', '--whitespace=nowarn', PATCH])
    if build.exists():
        shutil.rmtree(build)
    generator = ['-G', 'Ninja', f'-DCMAKE_MAKE_PROGRAM={ninja}', '-DCMAKE_BUILD_TYPE=Release'] if ninja else []
    options = ['-DGGML_NATIVE=OFF', '-DGGML_BACKEND_DL=ON', '-DGGML_CPU=OFF', '-DLLAMA_CURL=OFF', '-DLLAMA_BUILD_TESTS=OFF',
               '-DLLAMA_BUILD_EXAMPLES=OFF', '-DLLAMA_BUILD_TOOLS=OFF', '-DLLAMA_BUILD_SERVER=OFF', '-DLLAMA_BUILD_COMMON=OFF']
    configure = subprocess.list2cmdline([str(cmake), '-S', str(src), '-B', str(build), *generator, *options])
    compile_ = subprocess.list2cmdline([str(cmake), '--build', str(build), '--config', 'Release', '--target', 'llama', '-j'])
    script = HERE / 'work' / 'build-llama.cmd'
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f'@echo off\r\ncall "{vcvars}" x64 >nul || exit /b 1\r\n{configure} || exit /b 1\r\n{compile_} || exit /b 1\r\n', encoding='utf-8')
    run(['cmd', '/c', script])
    built = sorted(build.rglob('llama.dll'))
    if not built:
        raise SystemExit('llama.dll was not produced; see the build output above')
    if RUNTIME_FIX.exists():
        shutil.rmtree(RUNTIME_FIX)
    shutil.copytree(RUNTIME, RUNTIME_FIX)
    shutil.copyfile(built[0], RUNTIME_FIX / 'llama.dll')
    (RUNTIME_FIX / 'llama-fix.json').write_text(json.dumps({'base': info['asset'], 'commit': info['commit'], 'patch': PATCH.name,
                                                            'patch_sha256': sha256(PATCH), 'llama_dll_sha256': sha256(RUNTIME_FIX / 'llama.dll')},
                                                           indent=2), encoding='utf-8')
    say(f'patched runtime ready in {RUNTIME_FIX}; measure it with: run-spikes.bat bench --runtime fix --only spec-q8')


# ------------------------------------------------------------------------------------------------ bench

def gpu_device():
    """The llama.cpp device of the Intel Arc GPU (first Arc/B580 entry of --list-devices), else None (llama.cpp default)."""
    out = subprocess.run([str(RUNTIME / f'llama-server{EXE}'), '--list-devices'], capture_output=True, text=True, timeout=120)
    text = out.stdout + out.stderr
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / 'devices.txt').write_text(text, encoding='utf-8')
    for line in text.splitlines():
        m = re.match(r'\s*(\w+\d+)\s*:\s*(.+)', line)
        if m and re.search(r'Arc|B580|Battlemage', m.group(2), re.I):
            return m.group(1)
    return None


def ollama_loaded():
    try:
        with urllib.request.urlopen('http://127.0.0.1:11434/api/ps', timeout=2) as r:
            return [m.get('name') for m in json.loads(r.read().decode()).get('models', [])]
    except (OSError, ValueError):
        return []


def target_file(key):
    return SPEC[key] if key in SPEC else MODELS / LOCK['models'][key]['file']


def target_kind(key):
    return LOCK['built'][key]['kind'] if key in LOCK['built'] else LOCK['models'][key]['kind']


def bench(args):
    loaded = ollama_loaded()
    if loaded:
        say(f'WARNING: Ollama has models loaded on the GPU ({", ".join(loaded)}); close Ollama for clean memory figures')
    runtime = RUNTIME_FIX if args.runtime == 'fix' else RUNTIME
    if not (runtime / f'llama-server{EXE}').is_file():
        raise SystemExit(f'{runtime} is missing (run the step {"llama-fix" if args.runtime == "fix" else "fetch"})')
    suffix = '-fix' if args.runtime == 'fix' else ''
    device = gpu_device()
    say(f'GPU device: {device or "llama.cpp default"}')
    wanted = [t for t in (args.only.split(',') if args.only else TARGETS) if t]
    for key in wanted:
        if key == 'kev4b-q4' and not (args.with_kev or args.only):
            continue
        path = target_file(key)
        if not path.is_file():
            say(f'{key}: skipped, {path.name} is missing (run fetch/convert)')
            continue
        out = RESULTS / (key + suffix)
        if (out / 'target.json').is_file() and not args.force:
            say(f'{key}: already measured (use --force to measure again)')
            continue
        kind = target_kind(key)
        # Decision models output every prompt token (llama.cpp embedding mode), so memory grows with the micro-batch;
        # a Kev question with its options, and a whole Clef prompt, must fit in one micro-batch. Largest size first.
        batches = {'clef-flash-q4': [4096, 3072, 2048]}.get(key, [3072, 2048] if kind == 'systemone' else [2048])
        for batch in batches:
            if out.exists():
                shutil.rmtree(out)
            cmd = [sys.executable, HERE / 'bench.py', '--v1', args.v1, '--server', runtime / f'llama-server{EXE}', '--model', path,
                   '--kind', kind, '--out', out, '--banking', str(args.banking), '--batch', str(batch),
                   '--suites', 'jevbench,test2,banking77' if kind == 'systemone' else 'jevbench,test2']
            if args.limit:
                cmd += ['--limit', str(args.limit)]
            if kind == 'direct':
                cmd += ['--tokenizer', args.v1 / 'models' / 'qwen35-4b']
            if key == 'clef-flash-q4':
                cmd += ['--parallel', '1']       # 9B: one slot leaves room for the whole joint prompt in one batch
            if device:
                cmd += ['--device', device]
            try:
                run(cmd)
                break
            except SystemExit as exc:
                say(f'{key}: failed with batch {batch} ({exc}); ' + ('trying a smaller batch' if batch != batches[-1] else 'giving up'))


# ------------------------------------------------------------------------------------------------ main

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('step', nargs='?', default='all', choices=('all', 'fetch', 'convert', 'reference', 'bench', 'report', 'llama-fix', 'trio', 'trio-all', 'app', 'ui', 'phase3', 'release'))
    p.add_argument('--with-clef-variants', action='store_true', help='fetch: also the smaller Clef-Flash quantizations (bartowski Q4_K_M, IQ4_XS)')
    p.add_argument('--clef', default='clef-flash-iq4xs-b', help='trio: models.lock.json key of the Clef-Flash file')
    p.add_argument('--trio-eval', default='spec,plumb,clef', help='trio: roles whose suites are answered while all three are loaded')
    p.add_argument('--runtime', default='release', choices=('release', 'fix'), help='bench: the release llama.cpp or runtime/llama-fix')
    p.add_argument('--v1', type=Path, default=Path(os.environ.get('TYPELOGIC_V1') or V2.parent / 'TypeLogic-AI'))
    p.add_argument('--profile', type=Path, help='specialist profile (default: the active 1.x profile)')
    p.add_argument('--banking', type=int, default=1000, help='BANKING77 test rows per target (0 = all 2896)')
    p.add_argument('--with-kev', action='store_true', help='also fetch and measure the public Kev-4B (3 GB)')
    p.add_argument('--only', help='bench: comma-separated targets, e.g. spec-q8,clef-flash-q4')
    p.add_argument('--force', action='store_true', help='bench/reference: measure again')
    p.add_argument('--limit', type=int, help='smoke test: first N items of every suite')
    p.add_argument('--online', action='store_true', help='release: also download the published release assets')
    args = p.parse_args()
    args.v1 = args.v1.resolve()
    if not (args.v1 / 'typelogic' / 'engine.py').is_file():
        raise SystemExit(f'TypeLogic 1.x not found at {args.v1}; set TYPELOGIC_V1')
    if args.profile is None:
        active = json.loads((args.v1 / 'models' / 'active.json').read_text(encoding='utf-8'))['profile']
        args.profile = args.v1 / 'models' / active
    RESULTS.mkdir(parents=True, exist_ok=True)
    log = (RESULTS / 'spike.log').open('a', encoding='utf-8')
    sys.stdout = Tee(sys.__stdout__, log)
    sys.stderr = Tee(sys.__stderr__, log)
    say(f'== {args.step}  (v1: {args.v1}, profile: {args.profile.name}, banking rows: {args.banking or "all"})')
    steps = (['fetch', 'convert', 'reference', 'bench', 'report'] if args.step == 'all' else
             ['fetch', 'trio-all', 'report'] if args.step == 'trio-all' else
             ['app', 'report'] if args.step == 'app' else ['ui', 'report'] if args.step == 'ui' else
             ['phase3', 'report'] if args.step == 'phase3' else ['release', 'report'] if args.step == 'release' else [args.step])
    if args.step == 'trio-all':
        args.with_clef_variants = True
    for step in steps:
        say(f'-- {step}')
        if step == 'fetch':
            fetch_runtime()
            fetch_models(args.with_kev, args.with_clef_variants)
        elif step == 'convert':
            convert(args)
        elif step == 'reference':
            out = RESULTS / 'torch'
            if args.force and out.exists():
                shutil.rmtree(out)
            # resumes: items already answered are skipped
            run([sys.executable, HERE / 'reference_torch.py', '--v1', args.v1, '--profile', args.profile, '--out', out,
                 '--banking', str(args.banking)] + (['--limit', str(args.limit)] if args.limit else []))
        elif step == 'bench':
            bench(args)
        elif step == 'app':
            if not (RUNTIME_FIX / f'llama-server{EXE}').is_file():
                llama_fix(args)
            run([sys.executable, HERE / 'app_verify.py', '--v1', args.v1, '--banking', str(args.banking)] + (['--limit', str(args.limit)] if args.limit else []))
        elif step == 'ui':
            if not (RUNTIME_FIX / f'llama-server{EXE}').is_file():
                llama_fix(args)
            run([sys.executable, HERE / 'app_verify.py', '--v1', args.v1, '--only', 'setup'])
            run([sys.executable, HERE / 'ui_smoke.py', '--v1', args.v1])
        elif step == 'phase3':
            # combined mode, Evaluate page and bias probes, Needle 3 (installed from the 1.x models/needle)
            if not (RUNTIME_FIX / f'llama-server{EXE}').is_file():
                llama_fix(args)
            run([sys.executable, HERE / 'app_verify.py', '--v1', args.v1, '--only', 'setup'])
            run([sys.executable, HERE / 'phase3_verify.py', '--v1', args.v1])
        elif step == 'release':
            # the release archive (tools/package.py) installed on a clean folder; --online after it is published
            if not (RUNTIME_FIX / f'llama-server{EXE}').is_file():
                llama_fix(args)
            run([sys.executable, HERE / 'app_verify.py', '--v1', args.v1, '--only', 'setup'])
            run([sys.executable, V2 / 'tools' / 'package.py', '--skip-build'])
            run([sys.executable, HERE / 'release_verify.py', '--v1', args.v1] + (['--online'] if args.online else []))
        elif step == 'llama-fix':
            llama_fix(args)
        elif step == 'trio-all':
            # unattended: every Clef-Flash file in turn (the first run that loads also answers with the specialist and Plumb);
            # a failure (out of memory, file not accepted) is recorded and the next variant still runs
            if not (RUNTIME_FIX / f'llama-server{EXE}').is_file():
                llama_fix(args)
            device = gpu_device()
            for clef in ['clef-flash-iq4xs-b', 'clef-flash-q4km-b', 'clef-flash-q4']:     # most likely to fit first
                if (RESULTS / f'trio-{clef}.json').is_file() and not args.force:
                    say(f'trio {clef}: already measured')
                    continue
                try:
                    run([sys.executable, HERE / 'trio.py', '--v1', args.v1, '--runtime', RUNTIME_FIX, '--clef', clef,
                         '--eval', 'clef' if (RESULTS / 'trio-plumb-q4' / 'test2.jsonl').is_file() else 'spec,plumb,clef',
                         '--banking', str(args.banking)]
                        + (['--device', device] if device else []) + (['--limit', str(args.limit)] if args.limit else []))
                except SystemExit as exc:
                    say(f'trio {clef}: failed ({exc}); continuing')
        elif step == 'trio':
            if not (RUNTIME_FIX / f'llama-server{EXE}').is_file():
                raise SystemExit('run the step llama-fix first (the specialist needs the patched llama.dll to fit)')
            device = gpu_device()
            run([sys.executable, HERE / 'trio.py', '--v1', args.v1, '--runtime', RUNTIME_FIX, '--clef', args.clef, '--eval', args.trio_eval,
                 '--banking', str(args.banking)] + (['--device', device] if device else []) + (['--limit', str(args.limit)] if args.limit else []))
        elif step == 'report':
            run([sys.executable, HERE / 'report.py', '--v1', args.v1, '--results', RESULTS, '--banking', str(args.banking)]
                + (['--limit', str(args.limit)] if args.limit else []))
    say('== done')


if __name__ == '__main__':
    main()
