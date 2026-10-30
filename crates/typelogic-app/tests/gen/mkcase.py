"""Reference outputs of TypeLogic 1.x for the Rust ports (casefmt, titles, evidence checks).

    python mkcase.py <TypeLogic-AI folder> > ../casefmt_fixture.json
"""
import json, sys
sys.path.insert(0, sys.argv[1])
from typelogic import casefmt, templates, titles, needle

def run(payload):
    try:
        internal, info = casefmt.normalize(payload)
        return {'payload': payload, 'internal': internal, 'info': {k: info[k] for k in ('missing_required', 'unconfirmed')}}
    except ValueError as e:
        return {'payload': payload, 'error': str(e)}

cases = []
for row in templates.load_all(sys.argv[1])['templates']:
    t = row['template']
    for values in (True, False):
        r = templates.to_request(t, values)
        r['case'] = r['case'] or t['title']
        cases.append(run(r))
        cases.append(run({**r, 'engine_input': 'json', 'attachments': [{'title': 'Note', 'content': '  Some text  '}]}))
base = {'case': 'A customer paid twice.', 'decisions': [{'output': 'boolean', 'instructions': 'Refund?'}]}
edges = [
    {}, {'case': '   ', 'decisions': []}, {'case': 'x', 'decisions': [{'output': 'maybe', 'instructions': 'q'}]},
    {'case': 'x', 'decisions': [{'output': 'choice', 'instructions': 'q', 'criteria': ['a']}]},
    {'case': 'x', 'decisions': [{'output': 'choice', 'instructions': 'q', 'criteria': ['a', 'b', 'a', 3, True, None, 1.5]}]},
    {'case': 'x', 'decisions': [{'output': 'choice', 'instructions': 'q', 'alternatives': {'a': 'A', 'b': None}, 'abstain_option': 'b'}]},
    {'case': 'x', 'decisions': [{'output': 'noul', 'instructions': 'q', 'criteria': {'true': ' yes ', 'false': ''}}]},
    {'case': 'x', 'decisions': [{'output': 'noul', 'instructions': 'q', 'criteria': {'maybe': 'x'}}]},
    {'case': 'x', 'decisions': [{'output': 'score', 'instructions': 'q', 'criteria': {'10': 'high', '2': 'low', '-1': 'neg', '2.5': 'mid'}}]},
    {'case': 'x', 'decisions': [{'output': 'score', 'instructions': 'q', 'levels': {'b': 'B', 'a': 'A'}}]},
    {'case': 'x', 'decisions': [{'output': 'score', 'instructions': 'q', 'criteria': 'abc'}]},
    {'case': 'x', 'decisions': [{'output': 'boolean', 'instructions': '  '}]},
    {'case': 'x', 'decisions': [{'output': 'boolean', 'instructions': 'q', 'name': 'Bad Name'}]},
    {'case': 'x', 'decisions': [{'output': 'boolean', 'instructions': 'q', 'name': 'same'}, {'output': 'boolean', 'instructions': 'q', 'name': 'same'}]},
    {'case': 'x', 'decision': {'output': 'boolean', 'instructions': 'q'}},
    {'case': {'summary': 'Short', 'details': 'Long details here'}, **{k: v for k, v in base.items() if k != 'case'}},
    {'case': {'summary': 'Inside', 'details': 'Inside the details'}, 'decisions': base['decisions']},
    {'case': {'text': 'Only text'}, 'decisions': base['decisions']},
    {**base, 'evidence': {'amount': 12.0, 'flag': True, 'note': 'café', 'zero': 0, 'none': None, 'big': 1e21, 'small': 0.1}},
    {**base, 'evidence_schema': [{'name': 'count', 'type': 'integer'}], 'evidence': {'count': 3.0}},
    {**base, 'evidence_schema': [{'name': 'count', 'type': 'integer'}], 'evidence': {'count': 3.5}},
    {**base, 'evidence_schema': [{'name': 'when', 'type': 'date'}], 'evidence': {'when': '2024-02-30'}},
    {**base, 'evidence_schema': [{'name': 'when', 'type': 'date', 'required': True}], 'evidence': {}},
    {**base, 'evidence_schema': [{'name': 'lvl', 'type': 'enum', 'options': ['a', 'b']}], 'evidence': {'lvl': 'c'}},
    {**base, 'evidence_schema': [{'name': 'lvl', 'type': 'enum', 'options': ['a']}]},
    {**base, 'evidence_schema': [{'name': 'Bad'}]},
    {**base, 'evidence_schema': [{'name': 'a', 'type': 'color'}]},
    {**base, 'evidence_schema': [{'name': 'a', 'unit': 'kg', 'description': ' weight ', 'required': 1}], 'evidence': {'a': {'value': 'x', 'source': 'extracted'}}},
    {**base, 'evidence': {'a': {'value': 'x', 'source': 'robot'}}},
    {**base, 'evidence': {'a': {'value': 'x', 'status': 'maybe'}}},
    {**base, 'evidence': {'a': {'value': 'x', 'check': 'in_text', 'status': 'unverified'}}},
    {**base, 'evidence': {'a': {'value': 'x', 'check': 'weird'}}},
    {**base, 'evidence': {'Bad Key': 1}},
    {**base, 'evidence': {"it's": 1}},
    {**base, 'evidence': {'a': [1, 2]}},
    {**base, 'evidence': {'a': 'x' * 1001}},
    {**base, 'engine_input': 'xml'},
    {**base, 'template': 5},
    {**base, 'space': 'team', 'threshold': 0.9, 'permutation_check': False, 'template': 'abc'},
    {**base, 'attachments': [{'type': 'image', 'content': 'x'}]},
    {**base, 'attachments': [{'type': 'document', 'content': ' '}]},
    {**base, 'attachments': [{'content': 'c', 'title': ''}]},
    {**base, 'engine_input': 'json', 'evidence': {'b': 2, 'a': 1.25}},
    {'case': 'line one\n\n  line two  ', 'evidence': {'z': 'last', 'a': False}, 'decisions': base['decisions']},
]
cases += [run(p) for p in edges]
tag_cases = []
for tags in (None, [], ['Billing', 'billing', '  VIP   Customer '], ['a'] * 9, ['x' * 33], ['<b>'], [5], 'billing', ["it's"]):
    try:
        tag_cases.append({'tags': tags, 'out': casefmt.validate_tags(tags)})
    except ValueError as e:
        tag_cases.append({'tags': tags, 'error': str(e)})
title_cases = []
for t in (None, '', '   ', '  Duplicate   charge  ', 'x' * 121, 5):
    try:
        title_cases.append({'title': t, 'out': casefmt.validate_title(t)})
    except ValueError as e:
        title_cases.append({'title': t, 'error': str(e)})
json.dump({'normalize': cases, 'tags': tag_cases, 'titles': title_cases}, sys.stdout, ensure_ascii=False)
