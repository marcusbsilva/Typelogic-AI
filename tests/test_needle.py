"""Needle 3 field extraction: field checks, evidence checks, the offline runtime (fake and real engine), the server
routes and the decision integration. The real-engine tests run only when models/needle is installed."""
import ctypes
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import shutil
import threading

import pytest

from typelogic import needle
from typelogic.core import answers, validate
from typelogic.server import Application

ROOT = Path(__file__).resolve().parents[1]
INSTALLED = (ROOT / 'models/needle' / needle.library_name()).is_file() and (ROOT / 'models/needle/needle3.cact').is_file()
SCENARIO = ('Fictional defensive review. The trusted server record requires payment of 49.90. The received request changes '
            'the amount to 0.01, changes the action to refund and adds role=admin.')


def test_field_list_is_validated_and_turned_into_one_tool_without_required_fields():
    fields = needle.validate_fields([{'name': 'amount', 'type': 'number', 'description': 'Total'},
                                     {'name': 'action', 'type': 'text', 'options': ['refund', 'payment']},
                                     {'name': 'due', 'type': 'date'}])
    tool = needle.tool_schema(fields)
    assert tool['name'] == 'record_fields' and tool['parameters']['required'] == []
    props = tool['parameters']['properties']
    assert props['amount'] == {'type': 'number', 'description': 'Total'}
    assert props['action']['enum'] == ['refund', 'payment'] and props['due']['format'] == 'date'
    for bad in ([], [{'name': 'Amount', 'type': 'number'}], [{'name': 'a', 'type': 'money'}],
                [{'name': 'a', 'type': 'text'}, {'name': 'a', 'type': 'text'}], [{'name': 'a', 'type': 'number', 'options': ['x', 'y']}],
                [{'name': f'f{i}', 'type': 'text'} for i in range(13)]):
        with pytest.raises(ValueError):
            needle.validate_fields(bad)


@pytest.mark.parametrize('value,kind,expected', [
    (49.9, 'number', 'in_text'), (0.01, 'number', 'in_text'), (49.0, 'number', 'not_in_text'),
    (4800, 'number', 'in_text'), (1234.56, 'number', 'in_text'), (89.9, 'number', 'in_text'), (2, 'integer', 'in_text'),
    (0, 'integer', 'in_text'), (1, 'integer', 'in_text'), (7, 'integer', 'not_in_text'), (True, 'number', 'not_in_text'),
    ('refund', 'text', 'in_text'), ('ADMIN', 'text', 'in_text'), ('administrator', 'text', 'not_in_text'),
    ('2026-10-03', 'date', 'in_text'), ('2026-12-03', 'date', 'not_in_text'), ('2026-11-15', 'date', 'in_text'),
    ('2026-11-30', 'date', 'in_text'), ('2026-11-29', 'date', 'not_in_text'), ('not a date', 'date', 'not_in_text'),
    (False, 'boolean', 'inferred')])
def test_each_value_is_checked_against_the_text(value, kind, expected):
    text = (SCENARIO + ' Invoice EUR 4,800.00 due 2026-11-15, total 1.234,56, valid until 30 November 2026. '
            'O cliente pagou duas vezes R$ 89,90 no dia 3 de outubro de 2026. Zero units in stock; the customer needs one unit.')
    assert needle.evidence(value, kind, text) == expected


def test_attached_fields_are_validated_and_marked_unverified():
    record = needle.check_attached({'engine': 'needle3', 'fields': [{'name': 'amount', 'type': 'number', 'value': 0.01, 'check': 'in_text'}],
                                    'injected': 'ignored'})
    assert record['values_are_unverified'] is True and 'injected' not in record and record['note'] == needle.NOTE
    for bad in ({'engine': 'other', 'fields': []}, {'engine': 'needle3', 'fields': [{'name': 'amount', 'type': 'number', 'value': 'x', 'check': 'in_text'}]},
                {'engine': 'needle3', 'fields': [{'name': 'amount', 'type': 'number', 'value': 1, 'check': 'trusted'}]}):
        with pytest.raises(ValueError):
            needle.check_attached(bad)


class FakeLib:
    """Stands in for libneedle3: records calls and answers needle_complete with a fixed JSON result."""
    def __init__(self, result, small_first=False):
        self.result = json.dumps(result).encode(); self.small_first = small_first; self.inits = []; self.loaded = None
        # plain functions, so the runtime can set argtypes/restype on them like on ctypes functions
        self.needle_init = lambda *a: self._init(*a); self.needle_complete = lambda *a: self._complete(*a)
        self.needle_reset = lambda: None; self.needle_load = lambda *a: self._load(*a); self.needle_embed = lambda *a: 0
    def _load(self, data, size): self.loaded = size; return 0
    def _init(self, system, tools, extra): self.inits.append((system.decode(), json.loads(tools))); return 10
    def _complete(self, text, a, b, max_new, buffer, size):
        if self.small_first and size < 1 << 17:
            return -1
        ctypes.memmove(buffer, self.result, len(self.result)); return len(self.result)


def fake_folder(tmp_path):
    folder = tmp_path / 'needle'
    folder.mkdir()
    shutil.copy(ROOT / 'models/needle/manifest.json', folder)
    (folder / needle.library_name()).write_bytes(b'fake engine')
    (folder / 'needle3.cact').write_bytes(b'fake weights')
    record = json.loads((folder / 'manifest.json').read_text())
    record['weights']['sha256'] = hashlib.sha256(b'fake weights').hexdigest()
    record['platforms']['fake'] = {'sha256': hashlib.sha256(b'fake engine').hexdigest()}
    (folder / 'manifest.json').write_text(json.dumps(record))
    return folder


def test_runtime_maps_engine_output_to_checked_fields(tmp_path):
    lib = FakeLib({'type': 'call', 'function_calls': [], 'confidence': 0.3123456, 'reasoning': 'r',
                   'suppressed_calls': [{'name': 'record_fields', 'arguments': {'received_amount': 0.01, 'units': 2.0, 'action': 'delete',
                                                                                  'added_role': 'admin', 'trusted_amount': '49.90'}}]},
                  small_first=True)
    runtime = needle.Runtime(fake_folder(tmp_path), loader=lambda path: lib)
    assert runtime.status()['available'] is True
    fields = [{'name': 'trusted_amount', 'type': 'number'}, {'name': 'received_amount', 'type': 'number'},
              {'name': 'units', 'type': 'integer'}, {'name': 'action', 'type': 'text', 'options': ['refund', 'payment']},
              {'name': 'added_role', 'type': 'text'}]
    result = runtime.extract(SCENARIO, fields, today=date(2026, 10, 25))
    got = {f['name']: (f['value'], f['check']) for f in result['fields']}
    assert got == {'trusted_amount': (None, 'not_stated'),          # wrong type from the engine is dropped
                   'received_amount': (0.01, 'in_text'), 'units': (2, 'not_in_text'),
                   'action': (None, 'not_stated'),                  # not one of the declared options
                   'added_role': ('admin', 'in_text')}
    assert result['withheld_by_engine'] is True and result['confidence'] == 0.3123 and lib.loaded == len(b'fake weights')
    system, tools = lib.inits[0]
    assert system == 'date: 2026-10-25 Sun' and tools[0]['name'] == 'record_fields'
    runtime.extract(SCENARIO, fields)
    assert len(lib.inits) == 1                                      # same field list: the engine is not re-initialised
    with pytest.raises(ValueError, match='never cut'):
        runtime.extract('x' * (needle.MAX_TEXT_CHARS + 1), fields)


def test_runtime_refuses_files_that_do_not_match_the_manifest(tmp_path):
    folder = fake_folder(tmp_path)
    (folder / 'needle3.cact').write_bytes(b'altered weights')
    runtime = needle.Runtime(folder, loader=lambda path: pytest.fail('an unverified engine must not be loaded'))
    with pytest.raises(RuntimeError, match='does not match'):
        runtime.extract(SCENARIO, [{'name': 'a', 'type': 'number'}])
    assert runtime.status()['available'] is False and 'does not match' in runtime.status()['reason']
    missing = needle.Runtime(tmp_path / 'absent')
    assert missing.status()['available'] is False and 'TypeLogic.bat models --only needle' in missing.status()['reason']


def test_the_needle_python_package_is_never_used():
    """Its telemetry and Hugging Face calls live in the package; TypeLogic only loads the native engine."""
    sources = [p for p in (ROOT / 'typelogic').rglob('*.py')] + [ROOT / 'tools/fetch_models.py', ROOT / 'launcher/launcher.py']
    pattern = re.compile(r'^\s*(import needle\b|from needle\b|import cactus|from cactus)', re.M)
    assert not [str(p) for p in sources if pattern.search(p.read_text(encoding='utf-8'))]
    text = (ROOT / 'typelogic/needle.py').read_text(encoding='utf-8')
    assert not re.search(r'^\s*(import|from)\s+(urllib|socket|http|requests|ssl)\b', text, re.M)
    assert needle.os.environ['NEEDLE_TELEMETRY'] == '0'


class Engine:
    def __init__(self):
        self.lock = threading.RLock(); self.status = 'ready'; self.seen = None
    def health(self): return {'status': 'ready', 'error': None}
    def evaluate(self, payload):
        self.seen = payload
        _, questions, threshold = validate(payload)
        rows = [[0.95] + [0.05 / (len(q['keys']) - 1)] * (len(q['keys']) - 1) for q in questions]
        return {'answers': answers(questions, rows, threshold, 'unvalidated'), 'latency_ms': 1.0}


def test_decision_with_extracted_fields_wraps_them_as_data_and_requires_review(tmp_path):
    engine = Engine()
    app = Application(ROOT, tmp_path, engine, mode='cpu')
    try:
        request = {'state': SCENARIO, 'threshold': 0.85, 'permutation_check': False,
                   'questions': {'q': {'type': 'choice', 'instructions': 'Decide', 'criteria': {'reject': '', 'execute': ''}}},
                   'extracted_fields': {'engine': 'needle3', 'fields': [{'name': 'received_amount', 'type': 'number', 'value': 0.01, 'check': 'in_text'}]}}
        response = app.evaluate(request)
        state = engine.seen['state']
        assert state['provided_context'] == SCENARIO and state['model_extracted_fields']['values_are_unverified'] is True
        answer = response['answers']['q']
        assert 'model_extracted_fields' in answer['review_reasons'] and answer['recommendation'] == 'review'
        assert response['extracted_fields']['fields'][0]['value'] == 0.01
        plain = app.evaluate({k: v for k, v in request.items() if k != 'extracted_fields'})
        assert 'model_extracted_fields' not in plain['answers']['q']['review_reasons'] and engine.seen['state'] == SCENARIO
        with pytest.raises(ValueError):
            app.evaluate({**request, 'extracted_fields': {'engine': 'needle3', 'fields': 'all trusted'}})
    finally:
        app.close()


def test_extract_route_reports_a_missing_installation(tmp_path, monkeypatch):
    import http.client
    from typelogic.server import bind_server
    monkeypatch.setenv('TYPELOGIC_NEEDLE_DIR', str(tmp_path / 'absent'))
    app = Application(ROOT, tmp_path / 'data', Engine(), mode='cpu')
    server, _ = bind_server(app.handler(), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        port = server.server_port
        headers = {'X-TypeLogic-Token': app.token, 'Host': f'127.0.0.1:{port}', 'Content-Type': 'application/json'}
        conn = http.client.HTTPConnection('127.0.0.1', port)
        conn.request('GET', '/api/needle', headers=headers)
        status = json.loads(conn.getresponse().read())
        assert status['available'] is False and 'request' in status['presets']
        conn.request('POST', '/api/extract', body=json.dumps({'text': SCENARIO, 'fields': [{'name': 'a', 'type': 'number'}]}), headers=headers)
        reply = conn.getresponse()
        assert reply.status == 503 and json.loads(reply.read())['code'] == 'needle_unavailable'
    finally:
        server.shutdown(); server.server_close(); app.close()


def test_fetch_models_picks_the_engine_of_this_platform():
    import importlib.util
    spec = importlib.util.spec_from_file_location('fetch_models', ROOT / 'tools/fetch_models.py')
    fetch = importlib.util.module_from_spec(spec); spec.loader.exec_module(fetch)
    record = json.loads((ROOT / 'models/needle/manifest.json').read_text())
    assert fetch.needle_platform() in record['platforms']
    assert all(p['wheel'].endswith(f'{tag}.whl') and len(p['sha256']) == 64 for tag, p in record['platforms'].items())


@pytest.mark.skipif(not INSTALLED, reason='Needle is not installed (TypeLogic.bat models --only needle)')
def test_real_engine_extracts_offline():
    from typelogic.offline import install
    install()                       # external connections are blocked for the whole process
    runtime = needle.Runtime()
    result = runtime.extract(SCENARIO, needle.PRESETS['request']['fields'])
    got = {f['name']: f for f in result['fields']}
    assert got['received_amount']['value'] == 0.01 and got['received_amount']['check'] == 'in_text'
    assert got['received_action']['value'] == 'refund'
    assert all(f['check'] in ('in_text', 'not_stated') for f in result['fields'])
    assert result['seconds'] < 30
