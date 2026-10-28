"""Case / Evidence / Decision requests, titles, tags, templates, deletion and the library archive (no model)."""
import json
from pathlib import Path
import threading

import pytest

from typelogic import casefmt, templates
from typelogic.core import answers, validate
from typelogic.server import Application
from typelogic.store import Store

ROOT = Path(__file__).resolve().parents[1]


class Engine:
    """Deterministic engine: the first option gets `top` (or the option named in `pick`)."""
    def __init__(self, top=0.95, pick=None):
        self.lock = threading.RLock(); self.status = 'ready'; self.top = top; self.pick = pick or {}; self.seen = None
    def health(self): return {'status': 'ready', 'error': None}
    def evaluate(self, payload):
        self.seen = payload
        _, questions, threshold = validate(payload)
        rows = []
        for q in questions:
            n = len(q['keys']); best = q['keys'].index(self.pick[q['id']]) if q['id'] in self.pick else 0
            rows.append([self.top if i == best else (1 - self.top) / (n - 1) for i in range(n)])
        return {'answers': answers(questions, rows, threshold, 'unvalidated'), 'latency_ms': 1.0}


def request(**changes):
    base = {'case': 'My payout failed three days in a row.',
            'evidence': {'payout_failed_days': 3, 'urgency': {'value': 'high', 'source': 'extracted', 'status': 'confirmed'}},
            'evidence_schema': [{'name': 'payout_failed_days', 'type': 'integer'}, {'name': 'urgency', 'type': 'enum', 'options': ['low', 'medium', 'high']}],
            'decisions': [{'output': 'choice', 'instructions': 'Which team?',
                           'criteria': {'billing': 'Payments', 'technical': 'Software', 'x': 'Not enough information'}, 'abstain_option': 'x'}],
            'permutation_check': False}
    base.update(changes)
    return base


def test_v2_request_becomes_state_and_questions():
    text, info = casefmt.normalize(request())
    assert text['state'] == 'My payout failed three days in a row.\n\nEvidence:\n- payout_failed_days: 3\n- urgency: high'
    assert 'title' not in text and info['case'] == 'My payout failed three days in a row.'
    assert text['case_request']['engine_input'] == 'text' and text['case_request']['decision_order'] == ['decision_1']
    internal, info = casefmt.normalize(request(engine_input='json', decisions=[
        {'output': 'boolean', 'instructions': 'Safe?', 'criteria': {'true': 'Safe', 'false': ' '}},
        {'output': 'choice', 'instructions': 'Team?', 'criteria': ['billing', 'technical']},
        {'name': 'intent', 'output': 'score', 'instructions': 'Intent?', 'levels': {'2': 'Evaluating', '1': 'Browsing', '10': 'Urgent'}}]))
    assert internal['state'] == {'case': 'My payout failed three days in a row.', 'evidence': {'payout_failed_days': 3, 'urgency': 'high'}}
    q = internal['questions']
    assert list(q) == ['decision_1', 'decision_2', 'intent']
    assert q['decision_1'] == {'type': 'noul', 'instructions': 'Safe?', 'criteria': {'true': 'Safe'}}
    assert q['decision_2']['criteria'] == {'billing': '', 'technical': ''}
    assert q['intent']['criteria'] == ['Browsing', 'Evaluating', 'Urgent']   # numeric level keys by value
    assert internal['case_request']['evidence']['urgency'] == {'value': 'high', 'source': 'extracted', 'status': 'confirmed'}
    assert 'auto_review' not in internal['case_request']
    validate(internal)


def test_earlier_requests_still_work():
    """case {summary, details} is merged; rules, exact checks, auto_review, verdict and review_mode are ignored."""
    internal, info = casefmt.normalize(request(case={'summary': 'Customer support request', 'details': 'My payout failed.'}, review_mode='auto',
                                               rules=[{'id': 'r', 'when': {'all': [{'field': 'a', 'op': 'eval'}]}}], exact_checks=[{'op': 'eval'}],
                                               decisions=[{'name': 'safe', 'output': 'boolean', 'instructions': 'Safe?', 'verdict': {True: 'approve', False: 'deny'},
                                                           'auto_review': {'threshold': 0.3, 'yes': 'execute'}}]))
    assert internal['state'].startswith('Customer support request\n\nMy payout failed.') and 'rules' not in internal['case_request']
    assert 'exact_checks' not in internal and 'review_mode' not in internal
    assert internal['questions']['safe'] == {'type': 'noul', 'instructions': 'Safe?'}


@pytest.mark.parametrize('change,message', [
    ({'evidence': {'payout_failed_days': 'three'}}, 'expected integer'),
    ({'evidence': {'urgency': 'extreme'}}, 'expected enum'),
    ({'evidence': {'Bad Name': 1}}, 'lowercase'),
    ({'case': '   '}, 'describe the case'),
    ({'decisions': [{'output': 'yes/no', 'instructions': 'x'}]}, 'output must be'),
    ({'decisions': [{'output': 'boolean'}]}, 'Decision 1 question: write it'),
    ({'decisions': [{'output': 'choice', 'instructions': 'x', 'criteria': {'a': ''}}]}, 'at least two options'),
    ({'decisions': [{'name': 'a', 'output': 'boolean', 'instructions': 'x'}, {'name': 'a', 'output': 'boolean', 'instructions': 'y'}]}, 'unique'),
    ({'attachments': [{'type': 'image', 'content': 'x'}]}, 'Image attachments are not supported'),
    ({'attachments': [{'content': 'x' * 40001}]}, 'never cut'),
])
def test_invalid_v2_requests_are_refused(change, message):
    with pytest.raises(ValueError, match=message):
        casefmt.normalize(request(**change))


@pytest.mark.parametrize('tags,message', [
    ('billing', 'list of up to'),
    ([f't{i}' for i in range(9)], 'list of up to'),
    ([7], 'each tag is a text'),
    (['   '], '1-32 characters'),
    (['x' * 33], '1-32 characters'),
    (['<b>'], 'without'),
])
def test_invalid_tags_are_refused(tags, message):
    with pytest.raises(ValueError, match=message):
        casefmt.validate_tags(tags)


def test_tags_and_titles_are_normalized():
    assert casefmt.validate_tags(['Billing', '  urgent   case ', 'billing', 'my-own tag']) == ['billing', 'urgent case', 'my-own tag']
    assert casefmt.validate_tags(None) == [] and len(casefmt.TAGS) >= 15 and len(set(casefmt.TAGS)) == len(casefmt.TAGS)
    assert casefmt.validate_tags(list(casefmt.TAGS[:8])) == list(casefmt.TAGS[:8])
    assert casefmt.validate_title('  Failed   payout ') == 'Failed payout' and casefmt.validate_title('   ') is None
    with pytest.raises(ValueError, match='up to 120'):
        casefmt.validate_title('x' * 121)


def test_title_is_kept_or_generated_and_tags_are_stored(tmp_path, monkeypatch):
    from typelogic import ollama, titles
    monkeypatch.setattr(ollama, 'tags', lambda: None)               # no Ollama: the title comes from the case text
    app = Application(ROOT, tmp_path, Engine(), mode='cpu')
    try:
        out = app.evaluate(request(title='  Payout   problem ', tags=['Billing', 'urgent', 'billing', 'vip customer']))
        assert out['title'] == 'Payout problem' and out['title_source'] == 'user'
        assert out['tags'] == ['billing', 'urgent', 'vip customer']
        record = app.store.get_decision(out['decision_id'])
        assert record['title'] == 'Payout problem' and record['request']['tags'] == ['billing', 'urgent', 'vip customer']
        assert record['request']['title'] == 'Payout problem' and record['integrity']['verified']
        assert app.store.export_decision(out['decision_id'], 'lite') == {
            'title': 'Payout problem', 'tags': ['billing', 'urgent', 'vip customer'],
            'questions': [{'question': 'Which team?', 'answer': 'billing'}]}
        generated = app.evaluate(request(case='Hello team, my payout has failed three days in a row and I need the money today.'))
        assert generated['title'] == 'My payout has failed three days' and generated['title_source'] == 'case text'
        assert generated['tags'] == [] and 'tags' not in app.store.get_decision(generated['decision_id'])['request']
        # With Ollama running and the model pulled, the model writes the title (asked after the evaluation)
        monkeypatch.setattr(ollama, 'tags', lambda: {'qwen3.5:4b'})
        monkeypatch.setattr(titles, '_ask', lambda model, text, timeout: titles.clean('"Failed payout for three days."'))
        modeled = app.evaluate(request())
        assert modeled['title'] == 'Failed payout for three days' and modeled['title_source'] == 'model:qwen3.5:4b'
        monkeypatch.setattr(titles, '_ask', lambda model, text, timeout: (_ for _ in ()).throw(OSError('timeout')))
        assert app.evaluate(request())['title_source'] == 'case text'      # a failing model falls back to the case text
        jev = app.evaluate({'state': 'Customer asks for a refund of order 42.', 'tags': ['refund'],
                            'questions': {'refund': {'type': 'noul', 'instructions': 'Refund?'}}})
        assert jev['title'] == 'Customer asks for a refund of order 42' and jev['tags'] == ['refund']
    finally:
        app.close()


def test_title_from_text():
    from typelogic import titles
    assert titles.from_text('Fictional defensive review. The trusted server record requires payment.') == 'Fictional defensive review'
    assert titles.from_text('Hi! Ok.\nThe printer on floor three keeps jamming every single morning since Monday') == 'The printer on floor three keeps jamming every'
    assert titles.from_text({'order': 42}) == '{"order": 42}'
    assert titles.clean('x' * 81) is None and titles.clean('  ') is None


def test_delete_one_or_many_and_legacy_status_column(tmp_path):
    import sqlite3
    path = tmp_path / 'db.sqlite3'
    old = sqlite3.connect(path)       # a database from a version with approve / deny review
    old.executescript("""CREATE TABLE decisions(id TEXT PRIMARY KEY,space TEXT NOT NULL,title TEXT NOT NULL,request TEXT NOT NULL,response TEXT NOT NULL,status TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE feedback(id TEXT PRIMARY KEY,decision_id TEXT NOT NULL REFERENCES decisions(id),question_id TEXT NOT NULL,target TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE audit(id INTEGER PRIMARY KEY,decision_id TEXT,action TEXT NOT NULL,details TEXT NOT NULL,created TEXT NOT NULL);
        INSERT INTO decisions VALUES('gone','default','Old','{"state":"s","questions":{}}','{"answers":{}}','dismissed','2026-01-01T00:00:00+00:00');
        INSERT INTO decisions VALUES('kept','default','Old','{"state":"s","questions":{}}','{"answers":{}}','approved','2026-01-02T00:00:00+00:00');""")
    old.commit(); old.close()
    db = Store(path)
    try:
        rows = db.decisions()
        assert [r['id'] for r in rows] == ['kept'] and 'status' not in rows[0] and rows[0]['integrity']['verified']
        key = db.add_decision({'state': 's', 'questions': {}}, {'answers': {}}, 'T', 'default')
        assert db.count() == 2 and db.stats() == {'decisions': 2, 'feedback': 0}
        assert db.delete(key) == {'id': key, 'deleted': True, 'external_action_executed': False}
        assert db.delete(key)['deleted'] is False and db.audit_trail(key) == []
        ids = [db.add_decision({'state': 's', 'questions': {}}, {'answers': {}}, 'T', 'default') for _ in range(2)]
        result = db.delete_many(ids + ['missing'])
        assert result['deleted'] == 2 and result['results'][-1] == {'id': 'missing', 'deleted': False, 'external_action_executed': False}
        with pytest.raises(ValueError):
            db.delete_many(ids + ids)
        with pytest.raises(ValueError):
            db.delete('')
    finally:
        db.close()


def test_library_limit_and_archive(tmp_path, monkeypatch):
    import zipfile
    from datetime import date
    from typelogic import archive
    monkeypatch.setattr(archive, 'LIMIT', 3)
    monkeypatch.setattr(archive, 'rar_executable', lambda: None)           # no WinRAR: a .zip with the same content
    project = tmp_path / 'project'
    project.mkdir()
    app = Application(ROOT, tmp_path / 'home', Engine(), mode='cpu')
    app.root = project
    try:
        with pytest.raises(ValueError, match='empty'):
            app.archive()
        ids = [app.evaluate(request(tags=['billing'], space=space))['decision_id'] for space in ('default', 'default', 'team b')]
        app.store.feedback(ids[0], 'decision_1', {'billing': 0.0, 'technical': 1.0, 'x': 0.0})
        info = app.archive_info()
        assert info['full'] and info['count'] == 3 and info['format'] == 'zip' and info['folder'] == str(project / 'data')
        assert info['name'] == date.today().strftime('%d-%m-%Y') + '.zip'
        with pytest.raises(archive.LibraryFull, match='maximum'):
            app.evaluate(request())
        assert app.store.count() == 3                                         # refused before evaluating or saving
        result = app.archive()
        assert result['decisions'] == 3 and result['format'] == 'zip' and app.store.count() == 0
        assert result['path'] == str(project / 'data' / info['name']) and app.store.export()['feedback'] == []
        with zipfile.ZipFile(result['path']) as z:
            names = set(z.namelist())
            manifest = json.loads(z.read('manifest.json'))
            first = json.loads(z.read(f'decisions/default/{ids[0]}.json'))
        assert names == {'manifest.json', f'decisions/default/{ids[0]}.json', f'decisions/default/{ids[1]}.json', f'decisions/team_b/{ids[2]}.json'}
        assert manifest['decisions'] == 3 and manifest['workspaces'] == ['default', 'team b'] and len(manifest['files_sha256']) == 3
        assert first['request']['tags'] == ['billing'] and first['integrity']['verified'] and first['feedback'][0]['target']['technical'] == 1.0
        assert [a['action'] for a in first['audit']] == ['created', 'feedback']
        assert app.evaluate(request())['decision_id']                          # room again
        assert archive.next_path(project / 'data', 'zip').name == date.today().strftime('%d-%m-%Y') + '-2.zip'
        assert not [p for p in (project / 'data').iterdir() if p.is_dir()]    # the temporary folder is gone
    finally:
        app.close()


def test_failed_archive_removes_nothing(tmp_path, monkeypatch):
    from typelogic import archive
    monkeypatch.setattr(archive, 'rar_executable', lambda: None)
    monkeypatch.setattr(archive, '_compress', lambda source, target, kind: (_ for _ in ()).throw(OSError('disk full')))
    app = Application(ROOT, tmp_path / 'home', Engine(), mode='cpu')
    app.root = tmp_path / 'project'
    try:
        app.evaluate(request())
        with pytest.raises(OSError, match='disk full'):
            app.archive()
        assert app.store.count() == 1 and not list((tmp_path / 'project' / 'data').glob('*.zip'))
    finally:
        app.close()


@pytest.mark.skipif(__import__('sys').platform == 'win32', reason='fake rar is a POSIX shell script')
def test_archive_uses_rar_when_installed(tmp_path, monkeypatch):
    """The rar command line: a (add) with -r -ep1, then t (test); a fake rar records the calls."""
    from typelogic import archive
    log = tmp_path / 'calls.txt'
    fake = tmp_path / 'rar'
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\nif [ "$1" = a ]; then echo rar > "$7"; fi\n')   # $7: the archive path
    fake.chmod(0o755)
    monkeypatch.setenv('TYPELOGIC_RAR', str(fake))
    app = Application(ROOT, tmp_path / 'home', Engine(), mode='cpu')
    app.root = tmp_path / 'project'
    try:
        app.evaluate(request())
        assert app.archive_info()['format'] == 'rar'
        result = app.archive()
        assert result['format'] == 'rar' and result['name'].endswith('.rar') and app.store.count() == 0
        assert Path(result['path']).read_text().strip() == 'rar' and Path(result['path']).parent == tmp_path / 'project' / 'data'
        calls = log.read_text().splitlines()
        assert calls[0].startswith('a -r -ep1 -m5 -idq -y ') and calls[0].endswith('/content/*') and calls[1].startswith('t -idq ')
    finally:
        app.close()


def test_builtin_templates_are_valid_and_evaluate(tmp_path):
    found = templates.load_all(ROOT, tmp_path)
    assert not found['errors'] and len(found['templates']) >= 14
    app = Application(ROOT, tmp_path, Engine(), mode='cpu')
    try:
        for row in found['templates']:
            response = app.evaluate(templates.to_request(row['template']))
            assert response['answers'] and list(response['answers'])[0] == 'decision_1'
            assert response['tags'] == row['template'].get('tags', []) and response['title']
            assert all(t in casefmt.TAGS for t in response['tags'])
    finally:
        app.close()


def test_saved_templates_go_to_project_and_data_folders(tmp_path):
    root = tmp_path / 'project'
    (root / 'templates' / 'builtin').mkdir(parents=True)
    (root / 'templates' / 'builtin' / 'taken.json').write_text('{}')
    template = {'id': 'my-routing', 'title': 'My routing', 'evidence_schema': [{'name': 'amount', 'type': 'number'}],
                'decisions': [{'output': 'choice', 'instructions': 'Which team?', 'criteria': ['a', 'b']}]}
    saved = templates.save(template, root, tmp_path / 'data')
    assert (root / 'templates/custom/my-routing.json').is_file() and (tmp_path / 'data/templates/my-routing.json').is_file()
    assert len(saved['saved']) == 2
    with pytest.raises(ValueError, match='already exists'):
        templates.save(template, root, tmp_path / 'data')
    templates.save({**template, 'title': 'Renamed'}, root, tmp_path / 'data', overwrite=True)
    assert templates.get('my-routing', root, tmp_path / 'data')['title'] == 'Renamed'
    with pytest.raises(ValueError, match='built-in'):
        templates.save({**template, 'id': 'taken'}, root, tmp_path / 'data')
    with pytest.raises(ValueError):
        templates.save({**template, 'decisions': []}, root, tmp_path / 'data', overwrite=True)


def test_cli_templates_and_v2_validation(tmp_path, capsys):
    from typelogic.cli import main
    assert main(['templates', '--data', str(tmp_path)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert any(t['id'] == 'ticket-routing' for t in listing['templates'])
    assert main(['templates', '--id', 'ticket-routing', '--data', str(tmp_path)]) == 0
    v2 = json.loads(capsys.readouterr().out)
    assert v2['case'].startswith('My payout') and 'name' not in v2['decisions'][0]
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({**v2, 'evidence': {'urgency': 'extreme'}}))
    assert main(['decide', '--input', str(bad), '--data', str(tmp_path)]) == 1      # refused before loading a model
    assert 'expected enum' in capsys.readouterr().err


def test_evidence_extraction_schema_checks_and_backends(monkeypatch):
    from typelogic import evidence, ollama
    schema = [{'name': 'payout_failed_days', 'type': 'integer'}, {'name': 'urgency', 'type': 'enum', 'options': ['low', 'high']},
              {'name': 'refund', 'type': 'boolean'}, {'name': 'amount', 'type': 'number'}]
    js = evidence.json_schema(casefmt.validate_schema(schema))
    assert js['required'] == ['payout_failed_days', 'urgency', 'refund', 'amount'] and js['additionalProperties'] is False
    assert {'type': 'null'} in js['properties']['urgency']['anyOf'] and js['properties']['urgency']['anyOf'][0]['enum'] == ['low', 'high']
    sent = {}

    def fake_chat(text, schema, model):
        sent.update(model=model)
        return {'payout_failed_days': 3.0, 'urgency': 'extreme', 'refund': True, 'amount': '12'}, {'model': model, 'num_ctx': 4096}
    monkeypatch.setattr(evidence, '_ollama_extract', fake_chat)
    monkeypatch.setattr(ollama, 'tags', lambda: {'qwen3.5:4b'})
    result = evidence.extract('My payout failed three days in a row; please refund.', schema)
    got = {f['name']: (f['value'], f['check']) for f in result['fields']}
    assert result['backend'] == 'ollama' and sent['model'] == 'qwen3.5:4b'
    assert got == {'payout_failed_days': (3, 'in_text'), 'urgency': (None, 'not_stated'), 'refund': (True, 'inferred'), 'amount': (None, 'not_stated')}
    assert all(f['status'] == 'unverified' for f in result['fields'])
    monkeypatch.setattr(ollama, 'tags', lambda: None)            # Ollama absent: auto falls back to Needle, which refuses long text
    with pytest.raises(ValueError, match='short texts only'):
        evidence.extract('x ' * 1000, schema)
    with pytest.raises(ValueError, match='never cut'):
        evidence.extract('x' * (evidence.MAX_TEXT_CHARS + 1), schema, 'ollama')
    with pytest.raises(ValueError, match='Declare the evidence'):
        evidence.extract('text', [])


def test_ollama_url_must_be_local(monkeypatch):
    from typelogic import ollama
    monkeypatch.setenv('TYPELOGIC_OLLAMA_URL', 'http://example.com:11434')
    with pytest.raises(PermissionError):
        ollama.url()
    assert ollama.tags() is None
    monkeypatch.setenv('TYPELOGIC_OLLAMA_URL', 'http://127.0.0.1:11500')
    assert ollama.url() == 'http://127.0.0.1:11500'


def test_confidence_like_jev(tmp_path):
    """Each answer carries confidence (the top probability); there is no review status or automatic review any more."""
    app = Application(ROOT, tmp_path, Engine(0.95, {'decision_1': 'true'}), mode='cpu')
    try:
        decisions = [{'output': 'boolean', 'instructions': 'Safe?'}, {'output': 'choice', 'instructions': 'Team?', 'criteria': ['a', 'b']}]
        out = app.evaluate(request(decisions=decisions, review_mode='auto'))
        assert out['answers']['decision_1']['confidence'] == pytest.approx(0.95)
        assert not {'decision_status', 'auto_review', 'review_rules', 'review_mode'} & set(out)
        assert app.settings_info() == {'extract_backend': 'auto', 'extract_model': 'qwen3.5:4b'}
        with pytest.raises(ValueError):
            app.update_settings({'review_mode': 'auto'})
    finally:
        app.close()


def test_http_delete_tags_and_archive_routes(tmp_path, monkeypatch):
    import http.client
    from http.server import ThreadingHTTPServer
    from typelogic import archive
    monkeypatch.setattr(archive, 'LIMIT', 1)
    monkeypatch.setattr(archive, 'rar_executable', lambda: None)
    app = Application(ROOT, tmp_path / 'home', Engine(), mode='cpu')
    app.root = tmp_path / 'project'
    server = ThreadingHTTPServer(('127.0.0.1', 0), app.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()

    def call(method, path, body=None):
        con = http.client.HTTPConnection('127.0.0.1', server.server_port)
        con.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                    headers={'X-TypeLogic-Token': app.token, 'Content-Type': 'application/json', 'Host': f'127.0.0.1:{server.server_port}'})
        r = con.getresponse(); data = json.loads(r.read()); con.close()
        return r.status, data
    try:
        assert call('GET', '/api/tags')[1]['suggested'] == list(casefmt.TAGS)
        status, first = call('POST', '/api/evaluate', request(tags=['urgent']))
        assert status == 200 and first['tags'] == ['urgent']
        status, full = call('POST', '/api/evaluate', request())
        assert status == 409 and full['code'] == 'library_full' and full['archive']['full']
        assert call('POST', '/api/archive', {})[0] == 422                      # needs {"confirm": true}
        assert call('GET', '/api/status')[1]['library_limit'] == 1
        status, done = call('POST', '/api/archive', {'confirm': True})
        assert status == 200 and done['decisions'] == 1 and done['name'].endswith('.zip')
        status, again = call('POST', '/api/evaluate', request())
        assert status == 200
        assert call('POST', '/api/delete', {'id': again['decision_id']})[1]['deleted'] is True
        assert call('POST', '/api/review', {'id': again['decision_id'], 'status': 'approved'})[0] == 404
        assert call('GET', '/api/queue')[0] == 404
    finally:
        server.shutdown(); server.server_close(); thread.join(); app.close()
