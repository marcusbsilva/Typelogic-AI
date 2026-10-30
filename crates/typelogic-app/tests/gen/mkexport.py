"""A small library exported by TypeLogic 1.x (Store.export), for the importer test.
    python mkexport.py <TypeLogic-AI> > ../v1_export.json"""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from typelogic.store import Store
from typelogic.core import validate, answers
from typelogic import casefmt
with tempfile.TemporaryDirectory() as d:
    s = Store(Path(d) / 'typelogic.sqlite3')
    ids = []
    for i, (case, p) in enumerate([('Payout failed three days; café ☕ in São Paulo.', 0.873), ('Order 1182 charged twice: 12.30 USD.', 1 / 3)]):
        payload, info = casefmt.normalize({'case': case, 'tags': ['billing'], 'evidence': {'amount': 12.3, 'n': 3},
                                           'decisions': [{'output': 'choice', 'instructions': 'Team?', 'criteria': {'a': 'A', 'b': None}},
                                                         {'output': 'score', 'instructions': 'Urgency?', 'criteria': ['low', 'high']}]})
        state, qs, t = validate(payload)
        resp = {'answers': answers(qs, [[p, 1 - p], [0.1, 0.9]], t, 'unvalidated'), 'model': 'fixture', 'latency_ms': 12.5}
        for a in resp['answers'].values():
            a['confidence'] = max(a['probabilities'].values())
        payload['title'] = f'Case {i}'; payload['tags'] = ['billing']
        ids.append(s.add_decision(payload, resp, f'Case {i} ✓', 'default'))
    s.feedback(ids[0], 'decision_1', {'a': 0, 'b': 1})
    print(json.dumps(s.export(), ensure_ascii=False))
