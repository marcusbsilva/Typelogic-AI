"""Behavioral checks for runtime fixes that do not touch the frozen training closure."""
import http.client
import json
import subprocess
import sys
import threading
from pathlib import Path


from typelogic.schema import validate, answers
from typelogic.server import Application
from typelogic.store import Store

ROOT = Path(__file__).resolve().parents[1]


def cli(tmp_path, *args):
    return subprocess.run([sys.executable, '-I', str(ROOT / 'tools/run_app.py'), *args], cwd=tmp_path,
                          text=True, encoding='utf-8', capture_output=True)


def request():
    return {'state': 'Customer asks for a refund', 'questions': {'route': {'type': 'choice', 'instructions': 'Route?',
            'criteria': {'A': 'Billing', 'B': 'Support'}}}}


class FakeEngine:
    def __init__(self):
        self.lock = threading.RLock(); self.payload = None

    def evaluate(self, payload):
        self.payload = payload; _, qs, t = validate(payload)
        return {'answers': answers(qs, [[.95, .05]], t, 'unvalidated'), 'external_action_executed': False}

    def health(self):
        return {'status': 'ready'}


def test_failed_cli_export_leaves_no_empty_output_and_retry_is_possible(tmp_path):
    data = tmp_path / 'data'; output = tmp_path / 'out.json'
    failed = cli(tmp_path, 'export', '--data', str(data), '--id', 'missing', '--output', str(output))
    assert failed.returncode == 1 and 'Decision not found' in failed.stderr
    assert not output.exists(), 'An empty file would block the no-overwrite retry'
    store = Store(data / 'typelogic.sqlite3')
    try:
        key = store.add_decision({'state': 'x'}, {'answers': {}}, 'Kept', 'default')
    finally:
        store.close()
    ok = cli(tmp_path, 'export', '--data', str(data), '--id', key, '--output', str(output))
    assert ok.returncode == 0 and json.loads(output.read_text())['id'] == key
    again = cli(tmp_path, 'export', '--data', str(data), '--id', key, '--output', str(output))
    assert again.returncode == 1 and json.loads(output.read_text())['id'] == key


def test_post_routes_ignore_query_string(tmp_path):
    from http.server import ThreadingHTTPServer
    app = Application(ROOT, tmp_path, FakeEngine())
    server = ThreadingHTTPServer(('127.0.0.1', 0), app.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        body = json.dumps({'extract_backend': 'needle'}).encode()
        con = http.client.HTTPConnection('127.0.0.1', server.server_port)
        con.request('POST', '/api/settings?space=default', body=body,
                    headers={'X-TypeLogic-Token': app.token, 'Content-Type': 'application/json',
                             'Host': f'127.0.0.1:{server.server_port}'})
        response = con.getresponse(); status = response.status; result = json.loads(response.read()); con.close()
        assert status == 200 and result['extract_backend'] == 'needle'
    finally:
        server.shutdown(); server.server_close(); thread.join(); app.store.close()
