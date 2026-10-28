"""Corrected decisions exported as training rows (no model)."""
import json
import subprocess
import sys
from pathlib import Path

from typelogic.learning import training_record
from typelogic.schema import answers, validate
from typelogic.store import Store

ROOT = Path(__file__).resolve().parents[1]


def payload(state='Invoice charged twice; refund requested.'):
    return {'state': state, 'questions': {
        'q1': {'type': 'choice', 'instructions': 'Who should review?', 'criteria': {'A': 'Billing', 'X': 'Insufficient evidence'}},
        'q2': {'type': 'noul', 'instructions': 'Was a refund requested?'}}}


def response(p1, p2):
    _, qs, t = validate(payload())
    return {'answers': answers(qs, [[p1, 1 - p1], [1 - p2, p2]], t, 'unvalidated')}


def test_training_rows_use_corrections(tmp_path):
    db = Store(tmp_path / 'db')
    first = db.add_decision(payload(), response(.8, .3), 'First', 'default')
    db.feedback(first, 'q2', {'true': 1, 'false': 0})
    corrected = db.add_decision(payload('Second case'), response(.9, .9), 'Corrected', 'default')
    db.feedback(corrected, 'q1', {'A': 0, 'X': 1})
    db.feedback(corrected, 'q1', {'A': 1, 'X': 0})          # the latest correction wins
    db.add_decision(payload('Uncorrected'), response(.6, .6), 'Plain', 'default')
    result = db.training_records('default')
    rows = {r['decisions'][-1]: r for r in result['records']}
    assert set(rows) == {first, corrected} and result['skipped'] == {'integrity_failed': 0, 'uncorrected': 1}
    assert rows[first]['targets'] == {'q2': {'true': 1, 'false': 0}} and rows[first]['provenance'] == {'q2': 'correction'}
    assert rows[corrected]['targets'] == {'q1': {'A': 1, 'X': 0}} and set(rows[corrected]['questions']) == {'q1'}
    assert result['corrections'] == 2 and 'approved_answers' not in result
    for row in rows.values():                               # every row is a valid training record
        assert training_record(row)['questions'] and row['group_id'].startswith('state-') and row['source_id'] == 'typelogic-feedback'
    again = db.add_decision(payload(), response(.2, .3), 'Same scenario again', 'default')   # same state as `first`
    db.feedback(again, 'q1', {'A': 0, 'X': 1})
    merged = [r for r in db.training_records('default')['records'] if first in r['decisions']]
    assert len(merged) == 1 and merged[0]['decisions'] == [first, again] and merged[0]['targets'] == {'q2': {'true': 1, 'false': 0}, 'q1': {'A': 0, 'X': 1}}
    with db.db:
        db.db.execute("UPDATE decisions SET title='tampered' WHERE id=?", (first,))
    assert db.training_records('default')['skipped']['integrity_failed'] == 1
    db.close()


def test_cli_training_export(tmp_path):
    data = tmp_path / 'data'
    db = Store(data / 'typelogic.sqlite3')
    key = db.add_decision(payload(), response(.6, .9), 'Case', 'default')
    db.close()

    def cli(*args):
        return subprocess.run([sys.executable, '-I', str(ROOT / 'tools/run_app.py'), *args, '--data', str(data)],
                              cwd=tmp_path, capture_output=True, text=True, encoding='utf-8')
    assert cli('queue').returncode != 0                     # the review queue was removed with review statuses
    assert cli('feedback', '--id', key, '--question', 'q2', '--target', '{"true": 0, "false": 1}').returncode == 0
    target = tmp_path / 'train.jsonl'
    out = cli('training-export', '--output', str(target))
    assert out.returncode == 0 and json.loads(out.stderr.strip().splitlines()[-1])['corrections'] == 1
    rows = [json.loads(line) for line in target.read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 1 and rows[0]['targets'] == {'q2': {'true': 0, 'false': 1}}
    assert cli('training-export', '--output', str(target)).returncode == 1   # never overwrites


def test_build_training_mix_adds_feedback_to_train_and_validation_only(tmp_path):
    def write(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')

    def row(i, source):
        return {'id': f'{source}-{i}', 'group_id': f'{source}-{i}', 'source_id': source, **payload(f'case {i}'),
                'targets': {'q1': {'A': 1, 'X': 0}, 'q2': {'true': 1, 'false': 0}}}
    for name in ('train', 'validation', 'calibration', 'test'):
        write(tmp_path / 'distill' / f'{name}.jsonl', [row(name, 'typelogic-distill-v1')])
        write(tmp_path / 'replay' / f'{name}.jsonl', [row(name + 'r', 'replay')])
    feedback = [row(i, 'typelogic-feedback') for i in range(40)]
    write(tmp_path / 'feedback.jsonl', feedback)
    out = subprocess.run([sys.executable, str(ROOT / 'tools/build_training_mix.py'), '--distill', str(tmp_path / 'distill'),
                          '--replay-train', str(tmp_path / 'replay/train.jsonl'), '--replay-splits', str(tmp_path / 'replay'),
                          '--feedback', str(tmp_path / 'feedback.jsonl'), '--out', str(tmp_path / 'mix')], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    manifest = json.loads((tmp_path / 'mix/manifest.json').read_text())
    counts = {k: v['feedback'] for k, v in manifest['splits'].items()}
    assert counts['calibration'] == counts['test'] == 0 and counts['train'] + counts['validation'] == 40 and 0 < counts['validation'] < 10
    assert manifest['feedback_rows'] == 40


def test_unreadable_record_is_listed_as_unverified_and_can_be_deleted(tmp_path):
    db = Store(tmp_path / 'db')
    good = db.add_decision(payload(), response(.6, .9), 'Good', 'default')
    bad = db.add_decision(payload(), response(.6, .9), 'Bad', 'default')
    with db.db:
        db.db.execute("UPDATE decisions SET response='{\"answers\": NaN' WHERE id=?", (bad,))
    rows = {r['id']: r for r in db.decisions('default')}
    assert rows[good]['integrity']['verified'] and not rows[bad]['integrity']['verified']
    assert 'unreadable' in rows[bad]['integrity']['error']
    assert db.training_records('default')['records'] == []
    assert db.delete(bad)['deleted'] and bad not in {r['id'] for r in db.decisions('default')}
    db.close()


def test_feedback_ids_are_checked(tmp_path):
    import pytest
    db = Store(tmp_path / 'db')
    key = db.add_decision(payload(), response(.6, .9), 'Case', 'default')
    with pytest.raises(ValueError):
        db.feedback(['not', 'a', 'key'], 'q1', {'A': 1, 'X': 0})
    with pytest.raises(ValueError):
        db.feedback(key, 7, {'A': 1, 'X': 0})
    db.close()
