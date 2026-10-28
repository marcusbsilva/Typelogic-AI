"""Serve the real Application (store, server, exports, Rust core) with a deterministic fake engine for UI tests."""
import argparse
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from typelogic.server import Application  # noqa: E402
from typelogic.core import validate, answers, backend  # noqa: E402
from typelogic import archive, evidence, modes, needle  # noqa: E402


class FakeEngine:
    """Deterministic distribution: the first alternative gets 0.9 (or the level/answer in the middle)."""
    def __init__(self):
        self.lock = threading.RLock()

    def evaluate(self, payload):
        _, questions, threshold = validate(payload)
        rows = []
        for q in questions:
            n = len(q['keys'])
            rows.append([0.9 if i == 0 else 0.1 / (n - 1) for i in range(n)])
        return {'answers': answers(questions, rows, threshold, 'unvalidated'), 'model': 'fixture', 'latency_ms': 12.5, 'external_action_executed': False}

    def health(self):
        return {'status': 'ready', 'error': None, 'model': 'fixture', 'base_model': 'Qwen/Qwen3.5-2B (fixture)', 'device': 'cpu',
                'max_state_tokens': 2048, 'calibration': 'not_validated_for_your_domain', 'readout': 'rust',
                'onnx': {'runtime': 'typelogic-core test', 'providers': ['RustCPU']}, 'core': backend(),
                'model_dir': str(ROOT / 'models/decision-2b-distill-s1024'), 'cache_entries': 0}


def availability(settings, root=None):
    """Every mode available, so the UI test can exercise the selector without models, Ollama or a GPU."""
    return {'cpu': {'available': True, 'detail': 'Specialist on the processor'},
            'gpu': {'available': True, 'detail': 'Specialist on Intel Arc B580 (fixture)', 'backend': 'xpu', 'name': 'fixture'},
            'hybrid': {'available': True, 'detail': 'Generalist crh225/plumb-4b through Ollama + specialist', 'gpu_device': True,
                       'models': ['crh225/plumb-4b:latest', 'qwen3.5:9b'], 'ollama_model': settings['ollama_model']}}


def needle_status():
    return {'engine': 'needle3', 'available': True, 'reason': None, 'loaded': False, 'presets': needle.PRESETS,
            'limits': {'max_text_chars': needle.MAX_TEXT_CHARS, 'max_fields': needle.MAX_FIELDS}, 'types': list(needle.FIELD_TYPES)}


def needle_extract(text, fields, today=None):
    """Fixed model output; the evidence check is the real one."""
    fields = needle.validate_fields(fields)
    output = {'trusted_amount': None, 'received_amount': 0.01, 'received_action': 'refund', 'added_role': 'administrator'}
    rows = [{'name': f['name'], 'type': f['type'], 'value': output.get(f['name']),
             'check': 'not_stated' if output.get(f['name']) is None else needle.evidence(output[f['name']], f['type'], text)} for f in fields]
    return {'engine': 'needle3', 'fields': rows, 'chars': len(text), 'seconds': 0.31, 'confidence': 0.32,
            'withheld_by_engine': False, 'reasoning': '', 'engine_flags': {}, 'note': needle.NOTE}


def evidence_extract(text, schema, backend='auto', model=None):
    """Fixed suggestions for the ticket-routing fields; the evidence check is the real one."""
    values = {'payment_related': True, 'payout_failed_days': 3, 'urgency': 'high'}
    fields = [{'name': f['name'], 'type': f['type'], 'value': values.get(f['name']), 'status': 'unverified',
               'check': 'not_stated' if values.get(f['name']) is None else evidence._check(values[f['name']], f, text)} for f in schema]
    return {'backend': 'fixture', 'model': 'fixture-llm', 'fields': fields, 'chars': len(text), 'seconds': 0.2, 'note': 'fixture'}


class FixtureApplication(Application):
    def title_models(self):
        return []           # titles always come from the case text, whether or not Ollama runs on this computer

    def switch_mode(self, request):
        mode = modes.normalize(request.get('mode'))
        changes = {'mode': mode, **{k: request[k] for k in ('hybrid_device', 'hybrid_combine', 'ollama_model') if request.get(k)}}
        self.settings = modes.save_settings(self.data, changes)
        self.mode = mode
        return {'mode': mode, 'restarting': False, 'loading': False, 'note': None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--real-needle', action='store_true', help='Use the installed Needle engine (models/needle) instead of the fixture')
    parser.add_argument('--limit', type=int, help='Library limit for the archive test (default: the real 200)')
    args = parser.parse_args()
    archive.folder = lambda root: args.data / 'archives'      # never write archives into the project during tests
    archive.rar_executable = lambda: None
    if args.limit:
        archive.LIMIT = args.limit
    modes.availability = availability
    if not args.real_needle:
        needle.status, needle.extract = needle_status, needle_extract
    evidence.extract = evidence_extract
    app = FixtureApplication(ROOT, args.data, FakeEngine(), mode='cpu')
    server = ThreadingHTTPServer(('127.0.0.1', 0), app.handler())
    print(f'http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.store.close()


if __name__ == '__main__':
    main()
