"""Golden vectors for the Node.js and Lua bindings, computed with the Python *reference* (not the Rust core).

Each case is {method, params, response} where response is the exact JSON text api::call must return:
json.dumps({"ok": result}) or {"error": {"type", "message"}}, ensure_ascii=False, allow_nan=False.
Run: python sdk/testdata/make_golden.py  (rewrites sdk/testdata/golden.json)
"""
import hashlib
import json
import os
import sys
from pathlib import Path

os.environ['TYPELOGIC_CORE'] = 'python'
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from typelogic import schema, exports  # noqa: E402


def ok(value):
    return json.dumps({'ok': value}, ensure_ascii=False, allow_nan=False)


def run(fn):
    try:
        return ok(fn())
    except (ValueError, RuntimeError, OverflowError, TypeError) as exc:
        return json.dumps({'error': {'type': type(exc).__name__, 'message': str(exc)}}, ensure_ascii=False)


def validate(payload):
    state, questions, threshold = schema.validate(payload)
    return {'state': state, 'questions': questions, 'threshold': threshold}


cases = []
add = lambda method, params, fn: cases.append({'method': method, 'params': params, 'params_json': json.dumps(params, ensure_ascii=False), 'response': run(fn)})

payloads = [
    {'state': 'Customer was charged twice for one order', 'questions': {'route': {'type': 'choice', 'instructions': 'Which team?', 'criteria': {'A': 'Billing', 'B': 'Support', 'X': 'Insufficient evidence'}, 'abstain_option': 'X'}}},
    {'state': {'order': 42, 'paid': True, 'amount': '10.50', 'notes': ['é', '中文', '😀']}, 'questions': {'refund': {'type': 'noul', 'instructions': 'Refund due?', 'criteria': {'true': 'duplicate charge'}}, 'risk': {'type': 'score', 'instructions': 'Risk', 'criteria': ['low', 'medium', 'high']}}, 'threshold': 0.9},
    {'state': '　\x1c', 'questions': {}},
    {'state': 'x', 'questions': {'q': {'type': 'choice', 'instructions': 'i', 'criteria': ['A', 'A']}}},
    {'state': 'x', 'questions': {'q': {'type': 'noul', 'instructions': 'i'}}, 'threshold': True},
    {'state': 'x', 'questions': {'q': {'type': 'score', 'instructions': 'i', 'criteria': ['only']}}},
]
for p in payloads:
    add('validate', {'payload': p}, lambda p=p: validate(p))

_, qs, t = schema.validate(payloads[1])
rows = [[0.3, 0.7], [0.2, 0.5, 0.3]]
add('answers', {'questions': qs, 'rows': rows, 'threshold': t, 'calibration': 'unvalidated'}, lambda: schema.answers(qs, rows, t, 'unvalidated'))
add('answers', {'questions': qs, 'rows': [[0.5]], 'threshold': t, 'calibration': 'unvalidated'}, lambda: schema.answers(qs, [[0.5]], t, 'unvalidated'))

for value in [{'b': 1, 'a': [1.5, 2.25e-7, -0.0, 12345678901234567890]}, 'plain', None, [True, {'z': 'é\n '}]]:
    add('canonical', {'value': value}, lambda v=value: schema.canonical(v))
    add('canonical', {'value': value, 'compact': True}, lambda v=value: json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))

answers = schema.answers(qs, rows, t, 'unvalidated')
record = {'id': 'f' * 32, 'space': 'default', 'title': 'Refund', 'created': '2026-10-23T10:00:00+00:00', 'request': {**payloads[1], 'tags': ['billing', 'é tag']},
          'response': {'answers': answers, 'latency_ms': 3.5}}
record.update(exports.digests(record), baseline_origin='created')
tampered = {**record, 'title': 'Refund (edited)'}
for r in (record, tampered):
    add('digests', {'record': r}, lambda r=r: exports.digests(r))
    add('verification', {'record': r}, lambda r=r: exports.verification(r))
    add('lite', {'record': r}, lambda r=r: exports.lite(r))
for i, f in [('abc', 'full'), ('abc', 'lite'), ('', 'full'), ('abc', 'pdf')]:
    add('export_filename', {'id': i, 'format': f}, lambda i=i, f=f: exports.export_filename(i, f))

for algorithm, fn in [('sha256', hashlib.sha256), ('sha512', hashlib.sha512), ('md5', hashlib.md5), ('blake2s', hashlib.blake2s)]:
    for text in ['', 'abc', 'é中😀' * 50]:
        add('hash', {'algorithm': algorithm, 'text': text}, lambda fn=fn, text=text: fn(text.encode()).hexdigest())

out = ROOT / 'sdk/testdata/golden.json'
out.write_text(json.dumps({'generated_by': 'Python reference (TYPELOGIC_CORE=python)', 'cases': cases}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
print(len(cases), 'cases ->', out)
