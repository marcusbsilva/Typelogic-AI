"""Engine modes (CPU / GPU / Hybrid), switching from the web app and the launcher's environment choice (no model)."""
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

from typelogic import modes
from typelogic.server import Application

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('launcher', ROOT / 'launcher/launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ('TYPELOGIC_MODE', 'TYPELOGIC_ENGINE', 'TYPELOGIC_DEVICE', 'TYPELOGIC_GPU_RESTART', 'TYPELOGIC_HYBRID_MODE', 'TYPELOGIC_OLLAMA_MODEL'):
        monkeypatch.delenv(key, raising=False)


def test_mode_precedence_and_settings_file(tmp_path, monkeypatch):
    assert modes.resolve(None, tmp_path) == 'cpu'                     # default
    modes.save_settings(tmp_path, {'mode': 'hybrid', 'hybrid_combine': 'ensemble'})
    assert modes.resolve(None, tmp_path) == 'hybrid'
    assert modes.resolve('gpu', tmp_path) == 'gpu'                    # --mode wins
    monkeypatch.setenv('TYPELOGIC_DEVICE', 'xpu')
    assert modes.resolve(None, tmp_path) == 'gpu'                     # legacy variable before the stored choice
    monkeypatch.setenv('TYPELOGIC_MODE', 'cpu')
    assert modes.resolve(None, tmp_path) == 'cpu'
    stored = json.loads((tmp_path / 'settings.json').read_text())
    assert stored == {'mode': 'hybrid', 'hybrid_combine': 'ensemble'}  # only what was changed is written
    with pytest.raises(ValueError):
        modes.save_settings(tmp_path, {'mode': 'turbo'})
    (tmp_path / 'settings.json').write_text('{"mode": "warp", "hybrid_device": 7}')
    assert modes.load_settings(tmp_path)['mode'] == 'cpu' and modes.load_settings(tmp_path)['hybrid_device'] == 'cpu'
    assert modes.needs_gpu('hybrid', {'hybrid_device': 'gpu'}) and not modes.needs_gpu('hybrid', {'hybrid_device': 'cpu'})


def test_build_returns_the_engine_of_each_mode(tmp_path):
    cpu = modes.build('cpu', tmp_path)
    assert cpu.requested_device == 'cpu' and cpu.readout in ('onnx', 'rust', 'torch')
    gpu = modes.build('gpu', tmp_path)
    assert gpu.requested_device == 'xpu' and gpu.readout == 'torch'
    hybrid = modes.build('hybrid', tmp_path, {'hybrid_combine': 'ensemble', 'ollama_model': 'qwen3.5:9b'})
    assert hybrid.mode == 'ensemble' and hybrid.generalist.ollama_model == 'qwen3.5:9b'
    assert hybrid.specialist.requested_device == 'cpu' and hybrid.specialist.cpu_precision == 'bfloat16'
    assert modes.build('hybrid', tmp_path, {'hybrid_device': 'gpu'}).specialist.requested_device == 'xpu'


class FakeEngine:
    def __init__(self, name='fake'):
        self.lock = threading.RLock(); self.status = 'not_loaded'; self.error = None; self.name = name; self.model = object()
    def health(self): return {'status': self.status, 'error': self.error, 'name': self.name}
    def load(self): self.status = 'ready'


def test_switching_mode_rebuilds_engine_saves_choice_and_restarts_for_gpu(tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(modes, 'build', lambda mode, *a, **k: built.append(mode) or FakeEngine(mode))
    monkeypatch.setattr(modes, 'gpu_info', lambda refresh=False: {'available': False, 'reason': 'no GPU build here'})
    from typelogic import ollama
    started = []
    monkeypatch.setattr(ollama, 'ensure_running', lambda model, **k: started.append(model) or 'running')
    monkeypatch.setattr(ollama, 'unload_all', lambda: 0)
    first = FakeEngine('first')
    app = Application(ROOT, tmp_path, first, mode='cpu')
    try:
        result = app.switch_mode({'mode': 'hybrid', 'hybrid_combine': 'ensemble'})
        assert result['mode'] == 'hybrid' and not result['restarting'] and built == ['hybrid'] and started == ['crh225/plumb-4b']
        assert first.model is None                                   # old engine released
        app.load_thread.join(5)
        assert app.engine.health()['status'] == 'ready' and app.mode == 'hybrid'
        assert modes.load_settings(tmp_path)['hybrid_combine'] == 'ensemble'
        with pytest.raises(RuntimeError, match='no GPU build'):      # not launched by the launcher: clear error
            app.switch_mode({'mode': 'gpu'})
        restarted = threading.Event()
        app.restart = restarted.set
        monkeypatch.setenv('TYPELOGIC_GPU_RESTART', '1')
        assert app.switch_mode({'mode': 'gpu'}) == {'mode': 'gpu', 'restarting': True}
        assert restarted.wait(3) and app.restarting and modes.load_settings(tmp_path)['mode'] == 'gpu'
        info = app.mode_info()
        assert info['mode'] == 'hybrid' and set(info['availability']) == {'cpu', 'gpu', 'hybrid'}
        with pytest.raises(ValueError):
            app.switch_mode({'mode': 'fast'})
    finally:
        app.close()


def test_launcher_picks_the_environment_of_the_mode(tmp_path, monkeypatch):
    cpu = tmp_path / 'cpu/python.exe'; gpu = tmp_path / 'gpu/python.exe'
    cpu.parent.mkdir(); gpu.parent.mkdir(); cpu.write_text(''); gpu.write_text('')
    monkeypatch.setattr(launcher, 'cpu_python', lambda: cpu)
    assert launcher.app_python('cpu', {}) == (cpu, None)
    assert launcher.app_python('gpu', {'gpu_python': str(gpu)}) == (gpu, None)
    assert launcher.app_python('hybrid', {'hybrid_device': 'gpu', 'gpu_python': str(gpu)}) == (gpu, None)
    assert launcher.app_python('hybrid', {'gpu_python': str(gpu)}) == (cpu, None)
    monkeypatch.setattr(launcher, 'default_gpu_env', lambda: tmp_path / 'missing')
    assert 'setup --gpu' in launcher.app_python('gpu', {})[1]
    data = tmp_path / 'data'
    launcher.save_settings(data, {'mode': 'gpu'})
    assert launcher.load_settings(data) == {'mode': 'gpu'}


def test_launcher_start_restarts_in_gpu_environment_on_exit_code_75(tmp_path, monkeypatch):
    cpu = tmp_path / 'cpu'; gpu = tmp_path / 'gpu'
    monkeypatch.setattr(launcher, 'cpu_python', lambda: cpu)
    monkeypatch.setattr(launcher, 'gpu_python', lambda settings: gpu)
    monkeypatch.setattr(launcher, '_wait_port_free', lambda port, seconds=20: None)
    calls = []
    def call(cmd, env=None, watch=True):
        cmd = [str(c) for c in cmd]
        calls.append((cmd[0], cmd[cmd.index('--mode') + 1], '--no-browser' in cmd, env['TYPELOGIC_GPU_RESTART']))
        if len(calls) == 1:   # the web app switched to GPU: settings saved, exit code 75
            launcher.save_settings(tmp_path / 'data', {'mode': 'gpu'})
            return launcher.RESTART_EXIT_CODE
        return 0
    monkeypatch.setattr(launcher, 'wait_child', call)
    assert launcher.main(['start', '--mode', 'cpu', '--data', str(tmp_path / 'data')]) == 0
    assert calls == [(str(cpu), 'cpu', False, '1'), (str(gpu), 'gpu', True, '1')]


def test_cpu_mode_uses_the_pytorch_readout_when_onnx_is_missing(tmp_path, monkeypatch):
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.delenv('TYPELOGIC_READOUT', raising=False)
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name, *a: None if name in ('onnx', 'onnxruntime') else real(name, *a))
    assert modes.build('cpu', tmp_path).readout == 'torch'
    assert modes.build('hybrid', tmp_path).specialist.readout == 'torch'


def test_server_never_shares_a_port_with_another_program():
    import socket
    from typelogic.server import bind_server, LoopbackServer
    from http.server import BaseHTTPRequestHandler
    other = socket.socket(); other.bind(('127.0.0.1', 0)); other.listen(1)
    busy = other.getsockname()[1]
    try:
        server, note = bind_server(BaseHTTPRequestHandler, busy)
        try:
            assert server.server_port != busy and str(busy) in note and not LoopbackServer.allow_reuse_address
        finally:
            server.server_close()
    finally:
        other.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX signal path; on Windows os.kill(SIGINT) would terminate the test run')
def test_one_ctrl_c_stops_the_child_and_leaves_the_launcher(tmp_path):
    """POSIX path of wait_child: the child gets SIGINT (as from a terminal), stops cleanly, and Stopped is raised once."""
    import os, signal, subprocess, threading, time
    script = tmp_path / 'child.py'
    script.write_text("import time\ntry:\n    print('ready', flush=True)\n    time.sleep(60)\nexcept KeyboardInterrupt:\n    print('clean stop', flush=True)\n")
    out = tmp_path / 'out.txt'
    def interrupt():
        time.sleep(1.0)
        os.kill(os.getpid(), signal.SIGINT)   # the launcher itself gets Ctrl+C
    threading.Thread(target=interrupt, daemon=True).start()
    with open(out, 'w') as handle:
        real = subprocess.Popen
        def popen(args, **kw):
            return real(args, stdout=handle, stderr=subprocess.STDOUT, **{k: v for k, v in kw.items() if k not in ('stdout', 'stderr')})
        launcher.subprocess.Popen, saved = popen, launcher.subprocess.Popen
        try:
            with pytest.raises(launcher.Stopped):
                launcher.wait_child([sys.executable, str(script)])
        finally:
            launcher.subprocess.Popen = saved
    assert 'Traceback' not in out.read_text()
