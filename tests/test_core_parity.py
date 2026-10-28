"""The Rust core must reproduce the Python reference exactly: results, key order, floats and error messages."""
import json
import math
import os
import random
from pathlib import Path

import pytest

from typelogic import core, schema, exports

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(core.backend()['backend'] != 'rust', reason='Rust extension typelogic._core not built')
SEED = int(os.environ.get('TYPELOGIC_PARITY_SEED', '20261023'))
CASES = int(os.environ.get('TYPELOGIC_PARITY_CASES', '3000'))

SPACES = [' ', '\t', '\n', '\x0b', '\x0c', '\r', '\x1c', '\x1d', '\x1e', '\x1f', '\x85', '\xa0', ' ', ' ', ' ',
          ' ', ' ', ' ', ' ', '　', '​', '﻿']
ALPHABET = list('abcXYZ019_-.,:;"\'\\/{}[] ') + SPACES + ['é', 'ß', 'Ω', 'ж', '中', '٣', '१', '😀', '́', '\x00', '\x07', '\x7f', '×', '÷', 'İ', 'ﬁ']


def outcome(fn, *args):
    try:
        return ('ok', fn(*args))
    except Exception as exc:  # compare exception class and message
        return ('err', type(exc).__name__, str(exc))


def same(a, b):
    """Deep equality that also checks dict key order, float bits and bool/int distinction."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        return math.isnan(a) and math.isnan(b) or (a == b and math.copysign(1, a) == math.copysign(1, b))
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def check(rust_fn, python_fn, *args):
    got, want = outcome(rust_fn, *args), outcome(python_fn, *args)
    if got[0] == 'err' and got[1] == 'CoreUnsupported':
        return 'fallback'
    assert same(got, want), f'Rust {got!r} != Python {want!r} for {args!r}'[:4000]
    return got[0]


def rtext(rng, n=None):
    n = rng.randint(0, 12) if n is None else n
    return ''.join(rng.choice(ALPHABET) for _ in range(n))


def rfloat(rng):
    return rng.choice([0.0, -0.0, 1.0, 0.5, 1e16, 1e15, 1e-5, 1e-4, 5e-324, 1.7976931348623157e308, 0.1 + 0.2,
                       rng.uniform(-1e6, 1e6), rng.random() * 10 ** rng.randint(-30, 30), -rng.random()])


def rjson(rng, depth=0):
    kind = rng.randint(0, 7 if depth < 3 else 4)
    if kind == 0: return None
    if kind == 1: return rng.random() < .5
    if kind == 2: return rng.choice([0, -1, 7, 2 ** 63, -(2 ** 70), 10 ** 30, rng.randint(-10 ** 6, 10 ** 6)])
    if kind == 3: return rfloat(rng)
    if kind == 4: return rtext(rng)
    if kind == 5: return [rjson(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {rtext(rng, rng.randint(0, 5)): rjson(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def pick(rng, valid, invalid, p_invalid=.08):
    return rng.choice(invalid) if rng.random() < p_invalid else rng.choice(valid)


def rpayload(rng):
    questions = {}
    count = pick(rng, [1, 2, 3, 5], [0, 33])
    for i in range(count):
        kind = pick(rng, ['choice', 'noul', 'score'], ['bogus', None, 7])
        q = {'type': kind, 'instructions': pick(rng, ['Route?', 'Is it ok?', 'x' * 6000, rtext(rng) + 'q', 'Qual é o nível?'], [' ', '\u3000\x1c', 'x' * 6001, 7, ''])}
        if kind == 'choice':
            q['criteria'] = pick(rng, [['A', 'B'], {'A': 'Billing', 'B': None}, {'A': '', 'B': 'x' * 3000}, [rtext(rng, 3) + 'a', rtext(rng, 3) + 'b'],
                                       {f'opt{j}': rtext(rng) for j in range(rng.randint(2, 6))}, ['A' * 200, 'B']],
                                 [['A', 'A'], ['A'], {'A': 1, 'B': ''}, {' ': 'x', 'B': 'y'}, {'A' * 201: '', 'B': ''}, {'A': 'x' * 3001, 'B': ''}, 'AB', None, [1, 2]])
            if rng.random() < .4:
                q['abstain_option'] = pick(rng, ['A', 'B', 'opt1', None], ['Z', 1, True])
        elif kind == 'noul':
            q['criteria'] = pick(rng, [None, {}, {'true': 'fine'}, {'false': '', 'true': 'yes'}, [], '', 0, {'true': 'x' * 3000}],
                                 [{'maybe': 'x'}, {'true': 3}, 'x', {'true': 'x' * 3001}])
            if rng.random() < .05:
                q['abstain_option'] = 'true'
        elif kind == 'score':
            q['criteria'] = pick(rng, [['low', 'high'], [rtext(rng, 2) + 'l' + str(j) for j in range(rng.randint(2, 6))], ['x' + str(j) for j in range(32)]],
                                 [['low'], ['a', 'a'], ['a', ' '], ['x'] * 33, None, ['a', 1]])
        key = pick(rng, [f'q{i}', f'question_{i}', 'k' * 120, (rtext(rng, 4) or 'q') + str(i)], ['', 'k' * 121])
        questions[key] = q if rng.random() > .02 else 'not a dict'
    state = pick(rng, ['Customer asks for a refund', rjson(rng), {'a': [1, 2.5], 'b': 'é'}, rtext(rng, 20) + 's', 'x' * 120000, [1e16, -0.0, 10 ** 30]],
                 ['   ', '\x1c\x1d', '', 'x' * 120001, float('nan'), {'n': float('inf')}])
    payload = {'state': state, 'questions': questions}
    if rng.random() < .5:
        payload['threshold'] = pick(rng, [.5, .85, 1, 1.0, .99999], [.49, 1.0000001, True, '0.9', None, 0, 10 ** 400, float('nan'), 2], .3)
    if rng.random() < .2:
        payload['permutation_check'] = pick(rng, [True, False], [1, None, 'yes'], .4)
    if rng.random() < .02:
        payload = rng.choice([[], 'x', {'questions': {}}, None])
    return payload


def corpus_rows():
    rows = []
    for path in sorted((ROOT / 'research/data').glob('*/*.jsonl'))[:40]:
        with path.open(encoding='utf-8') as source:
            for i, line in enumerate(source):
                if i >= 400:
                    break
                if line.strip():
                    rows.append(json.loads(line))
    for path in sorted((ROOT / 'examples').rglob('*.json')):
        try:
            rows.append(json.loads(path.read_text(encoding='utf-8')))
        except ValueError:
            pass
    return rows


def test_validate_matches_reference_on_project_data():
    rows = corpus_rows()
    if len(rows) < 50:
        pytest.skip('Project datasets are not installed in this copy')
    results = [check(core._rust.validate, schema.validate, row) for row in rows]
    assert results.count('ok') > len(rows) // 2 and 'fallback' not in results


def test_validate_and_answers_match_reference_on_fuzzed_inputs():
    rng = random.Random(SEED)
    seen = {'ok': 0, 'err': 0, 'fallback': 0}
    for _ in range(CASES):
        payload = rpayload(rng)
        seen[check(core._rust.validate, schema.validate, payload)] += 1
        try:
            _, questions, threshold = schema.validate(payload)
        except Exception:
            continue
        rows = []
        for q in questions:
            n = len(q['keys']) + (rng.random() < .05)
            rows.append([rng.choice([0.0, rng.random(), rng.random() * 1e308, 1e-300, float('nan') if rng.random() < .02 else 0.3,
                                     -1e-9 if rng.random() < .02 else 0.7]) for _ in range(n)])
        if rng.random() < .05:
            rows = rows[:-1]
        check(core._rust.answers, schema.answers, questions, rows, threshold, rng.choice(['unvalidated', 'local_validation_not_general_guarantee']))
    assert seen['ok'] > 100 and seen['err'] > 100, seen


def test_canonical_matches_json_dumps():
    rng = random.Random(SEED + 1)
    for _ in range(CASES):
        value = rjson(rng)
        check(core._rust.canonical, schema.canonical, value)
        check(lambda v: core._rust.canonical(v, True), lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False), value)
    assert check(core._rust.canonical, schema.canonical, '\ud800') == 'fallback'
    assert core.canonical('\ud800') == schema.canonical('\ud800')


def records(rng, n):
    out = []
    for i in range(n):
        payload = {'state': rtext(rng, 30) or 'state', 'questions': {'route': {'type': 'choice', 'instructions': 'Route?', 'criteria': ['A', 'B', 'C']},
                   'ok': {'type': 'noul', 'instructions': 'OK?'}, 'lvl': {'type': 'score', 'instructions': 'Level', 'criteria': ['low', 'mid', 'high']}}}
        tags = rng.choice([None, 'absent', [], ['billing'], ['billing', 'é tag']])
        if tags != 'absent':
            payload['tags'] = tags
        _, qs, t = schema.validate(payload)
        rows = [[rng.random() for _ in q['keys']] for q in qs]
        if rng.random() < .2:
            rows[2] = [.4, .4, .2]  # score tie: smaller level ID wins
        response = {'answers': schema.answers(qs, rows, t, 'unvalidated'), 'model': 'x', 'latency_ms': rfloat(rng)}
        record = {'id': f'{i:032x}', 'space': rng.choice(['default', 'é']), 'title': rtext(rng, 8), 'created': '2026-10-23T10:00:00+00:00',
                  'request': payload, 'response': response}
        values = exports.digests(record)
        record.update(values, baseline_origin=rng.choice(['created', 'migrated_existing', None]))
        if rng.random() < .3:
            record['sha512'] = values['sha512'][:-1] + '0'
        if rng.random() < .1:
            record.pop('baseline_origin')
        if rng.random() < .1:
            record['md5'] = None
        out.append(record)
    return out


def test_record_digests_verification_and_lite_match_reference():
    rng = random.Random(SEED + 2)
    for record in records(rng, 400):
        for name in ('canonical_bytes', 'digests', 'verification', 'lite'):
            assert check(getattr(core._rust, name), getattr(exports, name), record) == 'ok', name
    for decision_id in ['abc', '', 'x' * 128, 'x' * 129, 'é', 5]:
        for fmt in ['full', 'lite', 'pdf']:
            check(core._rust.export_filename, exports.export_filename, decision_id, fmt)


def test_generic_json_contract_matches_python_objects():
    payload = {'state': 'Refund?', 'questions': {'r': {'type': 'choice', 'instructions': 'Route?', 'criteria': ['A', 'B']}}}
    response = json.loads(core._rust.call('validate', json.dumps({'payload': payload})))
    state, questions, threshold = schema.validate(payload)
    assert response == {'ok': {'state': state, 'questions': questions, 'threshold': threshold}}
    error = json.loads(core._rust.call('validate', json.dumps({'payload': {'state': ' '}})))
    assert error == {'error': {'type': 'ValueError', 'message': 'Scenario: 1–120,000 characters'}}


def test_float_repr_matches_cpython_on_random_bit_patterns():
    import struct
    rng = random.Random(SEED + 5)
    values = [struct.unpack('<d', struct.pack('<Q', rng.getrandbits(64)))[0] for _ in range(CASES * 20)]
    values += [2.0 ** e for e in range(-1074, 1024)] + [math.nextafter(2.0 ** e, 0) for e in range(-1000, 1024, 7)]
    values = [v for v in values if math.isfinite(v)]
    for start in range(0, len(values), 5000):
        chunk = values[start:start + 5000]
        assert core._rust.canonical(chunk, True) == json.dumps(chunk, separators=(',', ':'))
