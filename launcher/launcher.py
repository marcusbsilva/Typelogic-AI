"""TypeLogic AI launcher: setup, model download, start (CPU, GPU or Hybrid), verification and tools in one place.

Run it through TypeLogic.bat (Windows) or ./typelogic.sh (Linux/macOS). Without arguments it shows a menu.

    TypeLogic.bat                         menu
    TypeLogic.bat start                   start with the mode saved in the web app (CPU by default)
    TypeLogic.bat start --mode gpu        start on the GPU (Intel Arc) and remember the choice
    TypeLogic.bat start --mode hybrid     generalist through Ollama + specialist
    TypeLogic.bat setup [--gpu]           create the Python environment (.venv), or the GPU environment
    TypeLogic.bat models [...]            download and verify the models (tools/fetch_models.py options)
    TypeLogic.bat verify [--hybrid]       tests, real decisions and readout parity
    TypeLogic.bat cli <command> [...]     command line (decide, list, review, export, queue, ...)
    TypeLogic.bat settings [key=value]    show or change the saved settings
    TypeLogic.bat evaluate [--data F]      local evaluation on labeled cases (accuracy, NLL, Brier, ECE)
    TypeLogic.bat diagnose | test | build-native

Only the Python standard library is used, so the launcher runs before the environment exists. The app itself runs in
the environment that fits the mode: the CPU environment (portable runtime or .venv) or the GPU environment
(%USERPROFILE%\\TypeLogic-XPU, created by `setup --gpu`). When the mode is changed to GPU in the web app, the app saves the
choice and exits with code 75; the launcher then restarts it in the GPU environment.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == 'win32'
RESTART_EXIT_CODE = 75
MODES = ('cpu', 'gpu', 'hybrid')
TORCH_CPU_INDEX = 'https://download.pytorch.org/whl/cpu'
TORCH_XPU_INDEX = 'https://download.pytorch.org/whl/xpu'
XPU_PACKAGES = ['transformers==5.19.0', 'peft==0.21.2', 'accelerate', 'safetensors==0.8.0', 'huggingface-hub==1.33.0', 'numpy']
APP_PACKAGES = ['onnx==1.23.1', 'onnxruntime==1.30.0', 'pytest==9.1.1']


# ---------------------------------------------------------------- settings (same file as typelogic/modes.py)

def data_dir(value=None):
    value = value or os.environ.get('TYPELOGIC_DATA')
    return Path(value) if value else Path.home() / '.typelogic-ai'


def load_settings(data):
    try:
        value = json.loads((data / 'settings.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data, changes):
    settings = load_settings(data)
    settings.update(changes)
    data.mkdir(parents=True, exist_ok=True)
    tmp = data / 'settings.tmp'
    tmp.write_text(json.dumps(settings, indent=2), encoding='utf-8')
    os.replace(tmp, data / 'settings.json')
    return settings


# ---------------------------------------------------------------- Python environments

def _venv_python(folder):
    return Path(folder) / ('Scripts/python.exe' if WINDOWS else 'bin/python')


def cpu_python():
    """Interpreter of the CPU environment: the portable runtime when present, else .venv."""
    candidates = ([ROOT / 'runtime/windows/python.exe'] if WINDOWS else []) + [_venv_python(ROOT / '.venv')]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def default_gpu_env():
    return Path.home() / ('TypeLogic-XPU' if WINDOWS else '.typelogic-xpu')


def gpu_python(settings):
    candidate = Path(settings['gpu_python']) if settings.get('gpu_python') else _venv_python(default_gpu_env())
    return candidate if candidate.is_file() else None


def needs_gpu(mode, settings):
    return mode == 'gpu' or (mode == 'hybrid' and settings.get('hybrid_device') == 'gpu')


def app_python(mode, settings):
    """(interpreter, error) for running the app in `mode`."""
    if needs_gpu(mode, settings):
        found = gpu_python(settings)
        return (found, None) if found else (None, 'The GPU environment is missing. Run: TypeLogic.bat setup --gpu')
    found = cpu_python() or gpu_python(settings)
    return (found, None) if found else (None, 'The Python environment is missing. Run: TypeLogic.bat setup')


def system_python():
    """A full Python 3.10+ able to create virtual environments (never the embedded runtime or a venv)."""
    def usable(cmd):
        try:
            out = subprocess.run([*cmd, '-c', 'import sys,venv;print(sys.version_info[:2]>=(3,10))'], capture_output=True, text=True, timeout=30)
            return out.returncode == 0 and out.stdout.strip() == 'True'
        except (OSError, subprocess.SubprocessError):
            return False
    candidates = []
    if WINDOWS and shutil.which('py'):
        candidates += [['py', '-3.12'], ['py', '-3']]
    base = getattr(sys, '_base_executable', sys.executable)
    if not (Path(base).parent / f'python{sys.version_info[0]}{sys.version_info[1]}._pth').exists():
        candidates.append([base])
    candidates += [[name] for name in ('python3.12', 'python3', 'python') if shutil.which(name)]
    return next((c for c in candidates if usable(c)), None)


# ---------------------------------------------------------------- Ctrl+C
#
# On Windows, Ctrl+C normally reaches every process of the console: the launcher, the app (which then prints tracebacks
# from wherever it was) and cmd.exe, which later asks "Terminate batch job (Y/N)?". While the launcher waits for a child
# or reads a menu choice, it turns Ctrl+C into an ordinary key (console input without ENABLE_PROCESSED_INPUT), so no
# process receives the event. A Ctrl+C key press then stops the child once, cleanly: the child runs in its own process
# group and gets CTRL_BREAK, which the app handles as a normal shutdown.

class Stopped(Exception):
    """The user pressed Ctrl+C: leave the launcher."""


class CtrlCAsKey:
    """Context manager: Ctrl+C becomes a key on the Windows console. Inactive (keys is False) when stdin is not a console."""

    def __enter__(self):
        self.active, self.mode = False, None
        if WINDOWS:
            import ctypes
            from ctypes import wintypes
            self.k32 = ctypes.windll.kernel32
            self.handle = self.k32.GetStdHandle(-10)                 # STD_INPUT_HANDLE
            mode = wintypes.DWORD()
            if self.handle and self.k32.GetConsoleMode(self.handle, ctypes.byref(mode)):
                self.mode = mode.value
                self.active = bool(self.k32.SetConsoleMode(self.handle, self.mode & ~0x0001))   # ENABLE_PROCESSED_INPUT off
        return self

    def __exit__(self, *exc):
        if self.mode is not None:
            self.k32.SetConsoleMode(self.handle, self.mode)
        return False

    def ctrl_c(self):
        """True when Ctrl+C was pressed since the last call (other pending keys are discarded)."""
        import msvcrt
        pressed = False
        while msvcrt.kbhit():
            pressed = msvcrt.getwch() == '\x03' or pressed
        return pressed


def _stop(proc, quiet=False):
    if proc.poll() is not None:
        return proc.returncode
    if not quiet:
        print('\nStopping...', flush=True)
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
        return proc.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
        return proc.wait()


def wait_child(cmd, env=None, watch=True):
    """Run cmd and wait. With watch, one Ctrl+C stops the child cleanly and raises Stopped."""
    args = [str(c) for c in cmd]
    if not watch:
        return subprocess.call(args, cwd=ROOT, env=env)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0
    with CtrlCAsKey() as keys:
        proc = subprocess.Popen(args, cwd=ROOT, env=env, creationflags=flags)
        try:
            while True:
                try:
                    return proc.wait(timeout=0.15)
                except subprocess.TimeoutExpired:
                    pass
                if keys.active and keys.ctrl_c():
                    _stop(proc)
                    raise Stopped()
        except KeyboardInterrupt:          # POSIX, or stdin not a console: the child got the signal too
            old = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                _stop(proc)
            finally:
                signal.signal(signal.SIGINT, old)
            raise Stopped() from None


def read_line(prompt):
    """input() that treats Ctrl+C as a key on Windows (Stopped), so cmd.exe never asks to terminate the batch job."""
    with CtrlCAsKey() as keys:
        if not keys.active:
            try:
                return input(prompt)
            except (EOFError, KeyboardInterrupt):
                raise Stopped() from None
        import msvcrt
        print(prompt, end='', flush=True)
        text = ''
        while True:
            ch = msvcrt.getwch()
            if ch in ('\r', '\n'):
                print(flush=True)
                return text
            if ch == '\x03':
                print(flush=True)
                raise Stopped()
            if ch in ('\x00', '\xe0'):          # arrows and function keys come as two characters
                msvcrt.getwch()
            elif ch == '\x08':
                if text:
                    text = text[:-1]
                    print('\b \b', end='', flush=True)
            elif ch.isprintable():
                text += ch
                print(ch, end='', flush=True)


def run(cmd, env=None, check=True, watch=True):
    print('>', ' '.join(str(c) for c in cmd), file=sys.stderr, flush=True)   # stderr: tool output on stdout stays clean JSON
    code = wait_child(cmd, env=env, watch=watch)
    if check and code != 0:
        raise SystemExit(code)
    return code


def base_env(extra=None):
    env = dict(os.environ, ORT_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_TELEMETRY='1', NEEDLE_TELEMETRY='0', DO_NOT_TRACK='1',
               PYTHONUTF8='1')
    env.update(extra or {})
    return env


# ---------------------------------------------------------------- commands

def cmd_setup(args):
    if args.gpu:
        return setup_gpu(args)
    target = ROOT / '.venv'
    py = _venv_python(target)
    if not py.is_file():
        base = system_python()
        if not base:
            raise SystemExit('Python 3.12 (64-bit) was not found. Install it from https://www.python.org/downloads/ and run setup again.')
        run([*base, '-m', 'venv', str(target)])
    run([py, '-m', 'pip', 'install', '--upgrade', 'pip'])
    run([py, '-m', 'pip', 'install', '-r', ROOT / 'requirements.txt', '--extra-index-url', TORCH_CPU_INDEX])
    print('\nEnvironment ready in .venv. Next: TypeLogic.bat models')
    return 0


def setup_gpu(args):
    data = data_dir(args.data)
    settings = load_settings(data)
    folder = Path(args.env) if args.env else default_gpu_env()
    py = _venv_python(folder)
    if not py.is_file():
        base = system_python()
        if not base:
            raise SystemExit('Python 3.12 (64-bit) was not found. Install it from https://www.python.org/downloads/ and run again.')
        run([*base, '-m', 'venv', str(folder)])
        run([py, '-m', 'pip', 'install', '--upgrade', 'pip'])
        run([py, '-m', 'pip', 'install', 'torch==2.14.1', '--index-url', TORCH_XPU_INDEX])
        run([py, '-m', 'pip', 'install', *XPU_PACKAGES])
    run([py, '-m', 'pip', 'install', *APP_PACKAGES])
    run([py, '-c', "import torch; print('torch', torch.__version__, '| Intel GPU available:', torch.xpu.is_available())"])
    if args.env:
        save_settings(data, {'gpu_python': str(py)})
    elif settings.get('gpu_python'):
        save_settings(data, {'gpu_python': None})
    print('\nGPU environment ready. Start with: TypeLogic.bat start --mode gpu')
    return 0


def cmd_models(args, rest):
    py = cpu_python() or gpu_python(load_settings(data_dir(args.data)))
    if not py:
        raise SystemExit('The Python environment is missing. Run: TypeLogic.bat setup')
    return run([py, ROOT / 'tools/fetch_models.py', *rest], env=base_env(), check=False)


def _wait_port_free(port, seconds=20):
    import socket
    for _ in range(seconds * 4):
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', port))
                return
            except OSError:
                time.sleep(0.25)


def cmd_start(args):
    data = data_dir(args.data)
    changes = {}
    if args.mode:
        changes['mode'] = args.mode
    if args.hybrid_device:
        changes['hybrid_device'] = args.hybrid_device
    if args.ensemble is not None:
        changes['hybrid_combine'] = 'ensemble' if args.ensemble else 'route'
    if args.ollama_model:
        changes['ollama_model'] = args.ollama_model
    settings = save_settings(data, changes) if changes else load_settings(data)
    open_browser = not args.no_browser
    while True:
        mode = settings.get('mode') if settings.get('mode') in MODES else 'cpu'
        py, error = app_python(mode, settings)
        if error:
            print(error)
            if mode != 'cpu' and cpu_python():
                print('Starting in CPU mode instead; change the mode later in Engine & privacy.')
                mode, py = 'cpu', cpu_python()
            else:
                return 1
        env = base_env({'TYPELOGIC_GPU_RESTART': '1' if gpu_python(settings) else '0'})
        cmd = [py, ROOT / 'tools/run_app.py', '--mode', mode, '--port', str(args.port), '--data', str(data)]
        if not open_browser:
            cmd.append('--no-browser')
        print(f'Starting TypeLogic AI - mode {mode.upper()} - {py}', flush=True)
        try:
            code = wait_child(cmd, env=env)
        except Stopped:
            print('TypeLogic AI stopped.', flush=True)
            raise
        if code != RESTART_EXIT_CODE:
            return code
        settings = load_settings(data)
        open_browser = False          # the open page reloads by itself
        _wait_port_free(args.port)


def _app(args, extra_env=None):
    py, error = app_python('cpu', load_settings(data_dir(getattr(args, 'data', None))))
    if error:
        raise SystemExit(error)
    return py, base_env(extra_env)


def cmd_cli(args, rest):
    py, env = _app(args)
    return run([py, ROOT / 'tools/run_app.py', *rest], env=env, check=False, watch=False)   # cli may read stdin


def cmd_verify(args, rest):
    data = data_dir(args.data)
    settings = load_settings(data)
    py, env = _app(args, {'TYPELOGIC_OLLAMA_MODEL': os.environ.get('TYPELOGIC_OLLAMA_MODEL') or settings.get('ollama_model') or 'crh225/plumb-4b'})
    if '--hybrid' in rest:
        _ensure_ollama(settings)
    return run([py, ROOT / 'tools/verify_install.py', *rest], env=env, check=False)


def _ensure_ollama(settings):
    sys.path.insert(0, str(ROOT))
    from typelogic import ollama   # standard library only
    print('Ollama:', ollama.ensure_running(settings.get('ollama_model') or 'crh225/plumb-4b', models_dir=settings.get('ollama_models')))


def cmd_diagnose(args, rest):
    py, env = _app(args)
    code = run([py, ROOT / 'tools/diagnose.py', *rest], env=env, check=False)
    settings = load_settings(data_dir(args.data))
    print(json.dumps({'cpu_python': str(cpu_python() or ''), 'gpu_python': str(gpu_python(settings) or ''),
                      'settings': settings, 'ollama': _ollama_state()}, indent=2))
    return code


def _ollama_state():
    try:
        with urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=2) as response:
            return {'running': True, 'models': [m.get('name') for m in json.load(response).get('models', [])]}
    except (OSError, ValueError):
        return {'running': False}


DEFAULT_EVAL = ROOT / 'research/data/typelogic-distill-v1-test2/test.jsonl'


def cmd_evaluate(args, rest):
    """Local evaluation: accuracy, NLL, Brier and ECE of the specialist or the generalist on labeled cases."""
    p = argparse.ArgumentParser(prog='TypeLogic.bat evaluate', description=cmd_evaluate.__doc__)
    p.add_argument('--data', type=Path, default=DEFAULT_EVAL, help='Labeled JSONL with state, questions and targets '
                   '(default: the held-out test set, 615 questions)')
    p.add_argument('--engine', choices=('specialist', 'generalist'), help='Default: generalist in Hybrid mode, else specialist')
    p.add_argument('--limit', type=int, help='Evaluate only the first N cases')
    p.add_argument('--out', type=Path, help='Report file (default: <data folder>/evaluations/<engine>-<time>.json)')
    a = p.parse_args(rest)
    data = data_dir(args.data)
    settings = load_settings(data)
    mode = settings.get('mode') if settings.get('mode') in MODES else 'cpu'
    engine = a.engine or ('generalist' if mode == 'hybrid' else 'specialist')
    if not a.data.is_file():
        raise SystemExit(f'Labeled data not found: {a.data}')
    out = a.out or data / 'evaluations' / f'{engine}-{time.strftime("%Y%m%d-%H%M%S")}.json'
    cmd = ['--split', a.data, '--out', out] + (['--limit', a.limit] if a.limit else [])
    if engine == 'generalist':
        py, env = _app(args)
        _ensure_ollama(settings)
        cmd = [py, ROOT / 'tools/eval_split.py', *cmd, '--system', 'direct', '--model-dir', ROOT / 'models/qwen35-4b',
               '--ollama-model', settings.get('ollama_model') or 'crh225/plumb-4b']
    else:
        gpu = mode == 'gpu' and gpu_python(settings)
        py, env = (gpu, base_env()) if gpu else _app(args)
        cmd = [py, ROOT / 'tools/eval_split.py', *cmd, '--system', 'typelogic', '--device', 'xpu' if gpu else 'cpu']
    print(f'Local evaluation - {engine} on {a.data.name}' + (f' (first {a.limit} cases)' if a.limit else '') + f'\nReport: {out}\n', flush=True)
    code = run(cmd, env=env, check=False)
    if code == 0:
        print(f'\nReport saved to {out}. Accuracy is against the labels of this file; validate on cases from your own domain.')
    return code


def menu_evaluate():
    """Menu option: ask for the data, the engine and a limit, then run cmd_evaluate."""
    print('\nLocal evaluation on labeled cases (JSONL with state, questions and targets).')
    path = read_line(f'  Data file [Enter = held-out test set, {DEFAULT_EVAL.name}]: ').strip().strip('"')
    engine = read_line('  Engine: 1 specialist, 2 generalist (Ollama) [Enter = from the saved mode]: ').strip()
    limit = read_line('  Evaluate only the first N cases [Enter = all]: ').strip()
    rest = (['--data', path] if path else []) + (['--engine', {'1': 'specialist', '2': 'generalist'}[engine]] if engine in ('1', '2') else [])
    rest += ['--limit', limit] if limit.isdigit() else []
    return main(['evaluate', *rest])


def cmd_test(args, rest):
    py, env = _app(args)
    return run([py, '-m', 'pytest', 'tests', '-q', *rest], env=env, check=False)


def cmd_build_native(args):
    """Rust core and bindings (needs Rust; on Windows the MSVC toolchain and Visual Studio Build Tools)."""
    if not shutil.which('cargo'):
        raise SystemExit('Rust (cargo) was not found. Install it from https://rustup.rs and run again.')
    py = cpu_python()
    env = base_env({'PYO3_PYTHON': str(py)} if py else None)
    manifest = ROOT / 'rust/Cargo.toml'
    target = ROOT / 'rust/target/release'
    if WINDOWS:
        # the embeddable runtime has no python3.lib; PyO3 generates the import library itself
        run(['cargo', 'build', '--release', '--manifest-path', manifest, '-p', 'typelogic-py', '--features', 'generate-import-lib'], env=env)
        run(['cargo', 'build', '--release', '--manifest-path', manifest, '-p', 'typelogic-node', '-p', 'typelogic-lua', '-p', 'typelogic-cli'], env=env)
        copies = [('_core.dll', 'typelogic/_core.pyd'), ('typelogic_core_node.dll', 'sdk/typescript/native/typelogic_core.node'),
                  ('typelogic_core_lua.dll', 'sdk/lua/typelogic_core.dll')]
    else:
        run(['cargo', 'build', '--release', '--workspace', '--manifest-path', manifest], env=env)
        ext = 'dylib' if sys.platform == 'darwin' else 'so'
        copies = [(f'lib_core.{ext}', 'typelogic/_core.abi3.so'), (f'libtypelogic_core_node.{ext}', 'sdk/typescript/native/typelogic_core.node'),
                  (f'libtypelogic_core_lua.{ext}', 'sdk/lua/typelogic_core.so')]
    for source, dest in copies:
        (ROOT / dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target / source, ROOT / dest)
        print('copied', dest)
    if py:
        run([py, '-c', f"import sys; sys.path.insert(0, r'{ROOT}'); from typelogic import core; print(core.backend())"], env=env, check=False)
    return 0


def cmd_settings(args, rest):
    data = data_dir(args.data)
    if rest:
        changes = {}
        for item in rest:
            key, sep, value = item.partition('=')
            if not sep:
                raise SystemExit(f'Use key=value, for example mode=hybrid (got {item!r})')
            changes[key.strip()] = None if value.strip() in ('', 'null', 'none') else value.strip()
        save_settings(data, changes)
    print(json.dumps({'file': str(data / 'settings.json'), **load_settings(data)}, indent=2))
    return 0


# ---------------------------------------------------------------- menu

MENU = [
    ('Start (saved mode)', ['start']),
    ('Start on the CPU', ['start', '--mode', 'cpu']),
    ('Start on the GPU (Intel Arc)', ['start', '--mode', 'gpu']),
    ('Start in Hybrid mode (Ollama + specialist)', ['start', '--mode', 'hybrid']),
    ('Set up the Python environment', ['setup']),
    ('Set up the GPU environment (Intel Arc)', ['setup', '--gpu']),
    ('Download and verify the models', ['models']),
    ('Verify the installation (tests + real decisions)', ['verify', '--hybrid']),
    ('Diagnose', ['diagnose']),
    ('Show settings', ['settings']),
    ('Build the native Rust core', ['build-native']),
    ('Local evaluation on labeled cases', menu_evaluate),
]


def status_line():
    settings = load_settings(data_dir())
    parts = [f"mode: {str(settings.get('mode') or 'cpu').upper()}",
             f"environment: {'ready' if cpu_python() else 'missing'}",
             f"GPU environment: {'ready' if gpu_python(settings) else 'not installed'}",
             f"models: {'ready' if _models_ready() else 'missing'}"]
    return ' | '.join(parts)


def _models_ready():
    sys.path.insert(0, str(ROOT))
    try:
        active = json.loads((ROOT / 'models/active.json').read_text(encoding='utf-8')).get('profile', 'decision-2b-distill-s1024')
    except (OSError, ValueError):
        active = 'decision-2b-distill-s1024'
    folder = ROOT / 'models' / active
    return (folder / 'adapter/head.pt').is_file() and any((folder / 'backbone').glob('*.safetensors'))


def menu():
    while True:
        print('\nTypeLogic AI launcher\n' + status_line() + '\n')
        for i, (label, _) in enumerate(MENU, 1):
            print(f'  {i:>2}  {label}')
        print('   0  Exit')
        try:
            choice = read_line('\nChoose: ').strip()
        except Stopped:
            return 0
        if choice in ('0', 'q'):
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
            continue
        action = MENU[int(choice) - 1][1]
        try:
            action() if callable(action) else main(action)
        except SystemExit as exc:
            if exc.code not in (0, None):
                print(exc.code if isinstance(exc.code, str) else f'Finished with code {exc.code}.')
        except Stopped:
            return 0


# ---------------------------------------------------------------- entry point

def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        return menu()
    passthrough = {'models': cmd_models, 'verify': cmd_verify, 'cli': cmd_cli, 'diagnose': cmd_diagnose, 'test': cmd_test,
                   'settings': cmd_settings, 'evaluate': cmd_evaluate}
    if argv[0] in passthrough:   # the remaining arguments belong to the tool (for example cli decide --data ...)
        return passthrough[argv[0]](argparse.Namespace(data=os.environ.get('TYPELOGIC_DATA')), argv[1:])
    parser = argparse.ArgumentParser(prog='TypeLogic', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('start', help='Start the web app')
    p.add_argument('--data', help='Data folder (default ~/.typelogic-ai); holds the database and settings.json')
    p.add_argument('--mode', choices=MODES, help='cpu (default), gpu or hybrid; the choice is saved')
    p.add_argument('--hybrid-device', choices=('cpu', 'gpu'), help='Where the specialist runs in hybrid mode')
    p.add_argument('--ensemble', action=argparse.BooleanOptionalAction, default=None, help='Hybrid: combine both engines for up to 16 options')
    p.add_argument('--ollama-model', help='Ollama model of the generalist (default crh225/plumb-4b)')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--no-browser', action='store_true')
    p = sub.add_parser('setup', help='Create the Python environment (.venv) or, with --gpu, the Intel Arc environment')
    p.add_argument('--gpu', action='store_true')
    p.add_argument('--env', help='--gpu: folder of the GPU environment (default %%USERPROFILE%%\\TypeLogic-XPU)')
    p.add_argument('--data', help=argparse.SUPPRESS)
    for name, text in (('models', 'Download and verify the models (options of tools/fetch_models.py)'),
                       ('verify', 'Tests, real decisions and readout parity (--hybrid also uses the hybrid engine)'),
                       ('cli', 'Command line: decide, list, review, export, queue, training-export, ...'),
                       ('diagnose', 'Dependencies, model files, environments and Ollama'),
                       ('test', 'Run the Python test suite'),
                       ('evaluate', 'Local evaluation on labeled cases (accuracy, NLL, Brier, ECE)'),
                       ('settings', 'Show settings, or change them with key=value')):
        sub.add_parser(name, help=text)
    sub.add_parser('build-native', help='Build the Rust core and its bindings (needs Rust)')
    args = parser.parse_args(argv)
    return {'start': cmd_start, 'setup': cmd_setup, 'build-native': cmd_build_native}[args.command](args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Stopped:
        sys.exit(0)
