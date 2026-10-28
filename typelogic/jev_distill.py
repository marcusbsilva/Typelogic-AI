"""Offline-safe helpers for an explicit Jev research run; never imported by serving."""

import json
import math
import urllib.error
import urllib.request

from .learning import training_record
from .schema import validate


DECISIONS_URL = 'https://openrouter.ai/api/alpha/decisions'
MODEL = 'typesafe/jev-1.13'


class TeacherPayloadError(ValueError):
    def __init__(self, message, response):
        super().__init__(message)
        self.response = {k: response[k] for k in ('model','id','provider','answers','usage')
                         if isinstance(response,dict) and k in response}


def normalize_wire_probabilities(raw, keys):
    """Admit cent-rounded probabilities only within feasible rounding intervals.

    In addition to mathematical feasibility, cap sum correction at 0.05 as a
    research admission policy. Preserve wire values; normalization does not
    reconstruct the provider's unknown full-precision probabilities.
    """
    if not isinstance(raw,dict) or set(raw)!=set(keys):
        raise ValueError('Teacher alternative IDs mismatch')
    values=[raw[k] for k in keys]
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=1 for v in values):
        raise ValueError('Invalid teacher probabilities')
    total=math.fsum(values)
    if total<=0:raise ValueError('Invalid teacher probability sum')
    method='sum_normalization'
    if abs(total-1)>1e-6:
        rounded=all(abs(v*100-round(v*100))<=1e-7 for v in values)
        lower=math.fsum(max(0.,v-.005) for v in values)
        upper=math.fsum(min(1.,v+.005) for v in values)
        if not rounded or abs(total-1)>.05+1e-9 or not lower-1e-9<=1<=upper+1e-9:
            raise ValueError('Invalid teacher probability sum: '+str(total))
        method='cent_rounding_normalization'
    result={k:raw[k]/total for k in keys}
    return result, {'wire_probabilities':dict(raw),'wire_sum':total,'method':method,
                    'normalization_l1':math.fsum(abs(result[k]-raw[k]) for k in keys),
                    'max_admission_sum_correction':.05}


def _transport(url, body, key):
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                 'X-Title': 'TypeLogic Research'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError('Decisions API HTTP ' + str(exc.code)) from None


def decide(row, api_key, transport=_transport):
    if not api_key:
        raise ValueError('OPENROUTER_API_KEY is required for research')
    if row.get('split') != 'train':
        raise ValueError('Teacher calls are restricted to training records')
    training_record(row)
    _, questions, _ = validate(row)
    if len(questions) != 1 or questions[0]['type'] != 'choice':
        raise ValueError('This pilot supports one Choice question per record')
    q = questions[0]
    body = {'model': MODEL, 'state': row['state'], 'questions': row['questions']}
    response = transport(DECISIONS_URL, body, api_key)
    try:
        return _parse_response(row,q,response)
    except ValueError as exc:
        raise TeacherPayloadError(str(exc),response) from None


def _parse_response(row,q,response):
    if not isinstance(response, dict) or not isinstance(response.get('answers'), dict):
        raise ValueError('Missing typed answers')
    if set(response['answers']) != {q['id']}:
        raise ValueError('Teacher question IDs mismatch')
    answer = response['answers'][q['id']]
    raw = answer.get('probabilities') if isinstance(answer, dict) else None
    if not isinstance(answer, dict) or answer.get('type') != 'choice' or not isinstance(raw, dict) or set(raw) != set(q['keys']):
        raise ValueError('Teacher alternative IDs mismatch')
    normalized,normalization=normalize_wire_probabilities(raw,q['keys'])
    values = [raw[k] for k in q['keys']]
    choice = answer.get('choice')
    if choice is not None and (choice not in raw or raw[choice] < max(values) - .005):
        raise ValueError('Teacher choice contradicts probabilities')
    usage = response.get('usage') or {}
    cost = usage.get('cost') if isinstance(usage, dict) else None
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0):
        raise ValueError('Invalid reported cost')
    return {'id': row['id'], 'source_id': row['source_id'], 'split': 'train',
            'model': response.get('model', MODEL), 'generation_id': response.get('id'),
            'probabilities': normalized, 'cost_usd': cost,'normalization':normalization}


def blend_training_record(row, probabilities, teacher_weight=.3):
    if row.get('split') != 'train':
        raise ValueError('Only training examples may be distilled')
    if isinstance(teacher_weight, bool) or not isinstance(teacher_weight, (float, int)) or not 0 <= teacher_weight <= 1:
        raise ValueError('Invalid teacher weight')
    _, questions, _ = validate(row)
    if len(questions) != 1 or questions[0]['type'] != 'choice':
        raise ValueError('One Choice question required')
    keys = questions[0]['keys'];qid = questions[0]['id']
    if not isinstance(probabilities, dict) or set(probabilities) != set(keys):
        raise ValueError('Teacher alternative IDs mismatch')
    values = [probabilities[k] for k in keys]
    if any(isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or p < 0 for p in values):
        raise ValueError('Invalid teacher distribution')
    total = math.fsum(values)
    if total <= 0 or abs(total - 1) > .005:
        raise ValueError('Invalid teacher distribution sum')
    normalized = {k: probabilities[k] / total for k in keys}
    gold = row['gold_label']
    if gold not in keys or row['targets'][qid].get(gold) != 1.:
        raise ValueError('Training gold label mismatch')
    agreement = max(keys, key=normalized.__getitem__) == gold
    output = dict(row)
    output['distillation'] = {'teacher_model': MODEL, 'needs_review': not agreement,
                              'teacher_weight': teacher_weight if agreement else 0.}
    if agreement:
        output['targets'] = dict(row['targets'])
        output['targets'][qid] = {k: (1 - teacher_weight) * float(k == gold) + teacher_weight * normalized[k] for k in keys}
    training_record(output)
    return output


def preflight_rows(splits, encode):
    """Check every item without holding encoded copies of the dataset in RAM."""
    result = {}
    for name, rows in splits.items():
        size = -1; longest_id = None; count = 0
        for row in rows:
            encoded = encode(row)
            count += 1
            if len(encoded['ids']) > size:
                size = len(encoded['ids']); longest_id = row.get('id')
        result[name] = {'rows': count, 'max_tokens': max(0, size), 'longest_id': longest_id}
    return result
