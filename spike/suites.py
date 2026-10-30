"""Evaluation suites of the v2 spikes, in the /v1/systemone payload shape, with gold answers.

Every engine (PyTorch reference, llama.cpp specialist, Clef-Flash, Plumb) answers the same requests. Results are
stored per question as a distribution over the canonical option keys:
    noul   ["false", "true"]
    choice the criteria keys, in their declared order
    score  ["0", "1", ...]
Suites (all already shipped with TypeLogic 1.x under research/data):
    jevbench  231 public JevBench items (MIT), one question each, gold = expected
    test2     213 held-out TypeLogic distillation records, 615 questions, gold = argmax of the teacher target
    banking77 BANKING77 test rows of intent-choice-en-v2 (CC-BY-4.0), 77 options, gold = gold_label
"""
import json
from pathlib import Path

SUITES = ('jevbench', 'test2', 'banking77')


def _lines(path):
    return [json.loads(s) for s in Path(path).read_text(encoding='utf-8').splitlines() if s.strip()]


def canonical_keys(question):
    t, c = question['type'], question.get('criteria')
    if t == 'noul':
        return ['false', 'true']
    if t == 'choice':
        return list(c) if isinstance(c, dict) else list(c)
    return [str(i) for i in range(len(c))]


def clean_question(question):
    """The /v1/systemone question: type, instructions, criteria (a Choice list becomes {key: null})."""
    out = {'type': question['type'], 'instructions': question['instructions']}
    c = question.get('criteria')
    if question['type'] == 'choice' and isinstance(c, list):
        c = {k: None for k in c}
    if c not in (None, {}, []):
        out['criteria'] = c
    return out


def load(v1_root, name, limit=None):
    """-> list of {'id', 'suite', 'state', 'questions': {qid: question}, 'gold': {qid: key}}"""
    data = Path(v1_root) / 'research' / 'data'
    items = []
    if name == 'jevbench':
        for tier in ('original', 'easy', 'hard'):
            for row in _lines(data / 'jevbench-public' / f'{tier}.jsonl'):
                q = row['question']
                if q['type'] == 'noul':
                    gold = {'yes': 'true', 'no': 'false'}[row['expected']]
                else:
                    gold = str(row['expected'])
                state = row['state'] if isinstance(row['state'], str) else json.dumps(row['state'], ensure_ascii=False, indent=1)
                items.append({'id': row['id'], 'suite': name, 'tier': tier, 'state': state, 'questions': {'q': clean_question(q)}, 'gold': {'q': gold}})
    elif name == 'test2':
        for row in _lines(data / 'typelogic-distill-v1-test2' / 'test.jsonl'):
            gold = {k: max(t, key=t.get) for k, t in row['targets'].items()}
            items.append({'id': row['id'], 'suite': name, 'state': row['state'],
                          'questions': {k: clean_question(q) for k, q in row['questions'].items()}, 'gold': gold})
    elif name == 'banking77':
        for row in _lines(data / 'intent-choice-en-v2' / 'test.jsonl'):
            if row.get('source_id') != 'banking77':
                continue
            items.append({'id': row['id'], 'suite': name, 'state': row['state'],
                          'questions': {k: clean_question(q) for k, q in row['questions'].items()}, 'gold': {'intent': row['gold_label']}})
    else:
        raise ValueError(f'unknown suite {name}')
    return items[:limit] if limit else items


def payload(item):
    return {'state': item['state'], 'questions': item['questions']}


def from_systemone(question, answer):
    """Distribution over canonical keys from a /v1/systemone answer."""
    keys = canonical_keys(question)
    if question['type'] == 'noul':
        p = float(answer['noul'])
        return [1.0 - p, p]
    probabilities = answer['probabilities']
    return [float(probabilities[k]) for k in keys]


def from_typelogic(question, answer):
    """Distribution over canonical keys from a TypeLogic 1.x answer (probabilities keyed by option key)."""
    return [float(answer['probabilities'][k]) for k in canonical_keys(question)]
