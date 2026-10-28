"""Evidence extraction: fill the evidence schema of a case from its free text, on this computer.

Backends:
  ollama  an instruction model in the local Ollama (default qwen3.5:4b) answers with JSON constrained to the schema
          (Ollama structured outputs). The context window is sized for the text; a text that does not fit is refused.
  needle  Needle 3 (typelogic/needle.py) for short texts only: on long documents it misses or misreads most fields.
  auto    ollama when the model is available, otherwise needle when the text is short.
Every value comes back unverified, with the same evidence check as Needle (written in the text, not found, inferred).
The values are suggestions for the reviewer: they enter a decision only after the user keeps them, and unconfirmed
ones keep the decision in review.
"""
import json
import math
import time

from . import casefmt, needle, ollama

BACKENDS = ('auto', 'ollama', 'needle')
DEFAULT_MODEL = 'qwen3.5:4b'
NEEDLE_MAX_CHARS = 1500          # Needle is used for short texts only
MAX_TEXT_CHARS = 60000           # about 20k tokens; longer text is refused, never cut
MAX_CONTEXT = 32768
SYSTEM = ('You extract structured evidence for a decision review. Fill each field with the value stated in the text. '
          'Copy numbers, dates (YYYY-MM-DD), names and codes exactly as written. Use null when the text does not state '
          'the value. Never compute, add, convert or guess a value, and never answer the decision itself.')


def _json_type(spec):
    kind = spec['type']
    base = {'boolean': {'type': 'boolean'}, 'number': {'type': 'number'}, 'integer': {'type': 'integer'},
            'text': {'type': 'string'}, 'date': {'type': 'string', 'format': 'date'},
            'enum': {'type': 'string', 'enum': spec.get('options', [])}}[kind]
    return {'anyOf': [base, {'type': 'null'}], 'description': spec.get('description') or spec['name'].replace('_', ' ')}


def json_schema(schema):
    return {'type': 'object', 'properties': {f['name']: _json_type(f) for f in schema},
            'required': [f['name'] for f in schema], 'additionalProperties': False}


def _check(value, spec, text):
    kind = spec['type']
    if kind in ('boolean', 'enum'):
        return 'inferred' if kind == 'boolean' or not (isinstance(value, str) and needle.evidence(value, 'text', text) == 'in_text') else 'in_text'
    return needle.evidence(value, kind, text)


def _clean(value, spec):
    """The value when it matches the field type, else None (a wrong type is dropped, not coerced)."""
    if value is None or isinstance(value, str) and not value.strip():
        return None
    try:
        casefmt._check_type(spec['name'], value, spec)
    except ValueError:
        if spec['type'] == 'integer' and isinstance(value, float) and value.is_integer():
            return int(value)
        return None
    if spec['type'] == 'integer' and isinstance(value, float):
        return int(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _ollama_extract(text, schema, model):
    names = ollama.tags()
    if names is None:
        raise RuntimeError('Ollama is not running (start Ollama or choose Needle for short texts)')
    if not ollama.has_model(names, model):
        raise RuntimeError(f'Model {model} is not in Ollama (run: ollama pull {model})')
    estimate = len(text) // 3 + 600 + 60 * len(schema)
    num_ctx = min(MAX_CONTEXT, max(4096, 1 << math.ceil(math.log2(estimate * 1.3))))
    if estimate > MAX_CONTEXT:
        raise ValueError('The text is too long for evidence extraction; extract from a shorter part (it is never cut automatically)')
    prompt = 'Fields:\n' + '\n'.join(f'- {f["name"]} ({f["type"]}' + (f': {", ".join(f["options"])}' if f.get('options') else '') + ')'
                                      + (f' - {f["description"]}' if f.get('description') else '') for f in schema) + f'\n\nText:\n<<<\n{text}\n>>>'
    body = {'model': model, 'stream': False, 'think': False, 'format': json_schema(schema),
            'options': {'temperature': 0, 'num_ctx': num_ctx},
            'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': prompt}]}
    import urllib.request
    request = urllib.request.Request(ollama.url() + '/api/chat', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=600) as response:
        reply = json.loads(response.read().decode('utf-8'))
    if reply.get('prompt_eval_count') and reply['prompt_eval_count'] >= num_ctx - 8:
        raise RuntimeError('The text filled the model context; extract from a shorter part')
    try:
        values = json.loads(reply['message']['content'])
    except (KeyError, ValueError) as exc:
        raise RuntimeError('The model did not return valid JSON') from exc
    if not isinstance(values, dict):
        raise RuntimeError('The model did not return a JSON object')
    return values, {'model': model, 'num_ctx': num_ctx, 'prompt_tokens': reply.get('prompt_eval_count')}


def _needle_extract(text, schema):
    if len(text) > NEEDLE_MAX_CHARS:
        raise ValueError(f'Needle extracts from short texts only (up to {NEEDLE_MAX_CHARS:,} characters); use the local LLM (Ollama) for this text')
    fields = [{'name': f['name'][:40], 'type': 'text' if f['type'] == 'enum' else f['type'], 'description': f.get('description', ''),
               **({'options': f['options']} if f['type'] == 'enum' else {})} for f in schema][:needle.MAX_FIELDS]
    if len(schema) > needle.MAX_FIELDS:
        raise ValueError(f'Needle reads up to {needle.MAX_FIELDS} fields at a time')
    result = needle.extract(text, fields)
    return {f['name']: f['value'] for f in result['fields']}, {'model': 'needle3', 'confidence': result['confidence']}


def available(model=DEFAULT_MODEL):
    names = ollama.tags()
    return {'ollama': {'running': names is not None, 'model': model, 'model_pulled': ollama.has_model(names, model) if names is not None else None,
                       'models': sorted(n for n in (names or ()) if n)},
            'needle': {'installed': needle.status()['available'], 'max_chars': NEEDLE_MAX_CHARS}}


def extract(text, schema, backend='auto', model=None):
    """{'backend', 'model', 'fields': [{name, type, value, check, status}], 'seconds', 'note'}."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Provide the case text to read')
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f'Evidence extraction reads up to {MAX_TEXT_CHARS:,} characters; this text has {len(text):,}. It is never cut automatically.')
    schema = casefmt.validate_schema(schema)
    if not schema:
        raise ValueError('Declare the evidence fields to extract (the template defines them)')
    if backend not in BACKENDS:
        raise ValueError('backend must be auto, ollama or needle')
    model = model or DEFAULT_MODEL
    if backend == 'auto':
        names = ollama.tags()
        backend = 'ollama' if names is not None and ollama.has_model(names, model) else 'needle'
    started = time.perf_counter()
    values, meta = _ollama_extract(text, schema, model) if backend == 'ollama' else _needle_extract(text, schema)
    fields = []
    for spec in schema:
        value = _clean(values.get(spec['name']) if spec['name'] in values else values.get(spec['name'][:40]), spec)
        fields.append({'name': spec['name'], 'type': spec['type'], 'value': value,
                       'check': 'not_stated' if value is None else _check(value, spec, text), 'status': 'unverified'})
    return {'backend': backend, **meta, 'fields': fields, 'chars': len(text), 'seconds': round(time.perf_counter() - started, 3),
            'note': 'Suggested by a local model. Unverified until you keep or correct each value.'}
