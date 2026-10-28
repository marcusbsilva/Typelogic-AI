"""Case / Evidence / Decision request format (v2), case titles and tags.

A v2 request separates what happened (the case, one text), the facts that matter (evidence, typed and with provenance)
and what must be decided (decisions, each one question in the Jev style: Yes / no, Choice or Score). It is converted
here into the established contract (state + questions), so the engines, the integrity checks and the exports keep
working unchanged:

    {"title": "Failed payout",                       (optional: a short title is generated when it is missing)
     "tags": ["billing", "urgent"],                  (optional: up to 8 tags, see TAGS for the suggested ones)
     "case": "My payout has failed three days in a row ...",
     "evidence": {"payout_failed_days": 3, "urgency": {"value": "high", "source": "extracted", "status": "unverified"}},
     "evidence_schema": [{"name": "urgency", "type": "enum", "options": ["low", "medium", "high"]}],
     "attachments": [{"type": "document", "title": "...", "content": "..."}],
     "decisions": [{"output": "choice", "instructions": "Which team should handle this message?",
                    "criteria": {"0": "Billing: payments, payouts, invoices, refunds", "1": "Technical: bugs, outages"}}]}

Decisions need no name: they are answered as decision_1, decision_2, ... unless a name is given. The engines read the
case text followed by one "name: value" line per evidence field (engine_input "text", the default) or one canonical
JSON object (engine_input "json"). Provenance stays in the stored request. Keys of earlier versions (auto_review,
verdict, review_mode) are accepted and ignored.
"""
from datetime import date
import math
import re

OUTPUTS = {'boolean': 'noul', 'noul': 'noul', 'choice': 'choice', 'score': 'score'}
EVIDENCE_TYPES = ('boolean', 'number', 'integer', 'date', 'text', 'enum')
SOURCES = ('user', 'extracted', 'template')
STATUSES = ('confirmed', 'unverified')
NAME = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
MAX_EVIDENCE = 64
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_CHARS = 40000
MAX_TEXT = 1000
PASSTHROUGH = ('space', 'threshold', 'permutation_check')
MAX_TAGS = 8
MAX_TAG_CHARS = 32
MAX_TITLE_CHARS = 120
# Suggested tags (the web app offers them while typing); any other tag can be typed too.
TAGS = ('billing', 'refund', 'fraud', 'security', 'compliance', 'legal', 'support', 'technical', 'sales', 'purchasing',
        'logistics', 'inventory', 'hr', 'finance', 'it-support', 'urgent', 'low-priority', 'escalation', 'customer', 'internal')
# engine_input (text | json) chooses how the case reaches the engines; see render_state.


def is_v2(payload):
    return isinstance(payload, dict) and any(k in payload for k in ('case', 'decision', 'decisions'))


def _text(value, label, limit, required=False):
    if value is None and not required:
        return ''
    if required and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f'{label}: write it (it is required)')
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'{label}: text up to {limit:,} characters')
    return value.strip()


def _scalar(value, name):
    if value is None or isinstance(value, bool) or isinstance(value, str) and len(value) <= MAX_TEXT:
        return value
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    raise ValueError(f'Evidence {name}: value must be text (up to {MAX_TEXT} characters), a finite number, true/false or null')


def validate_schema(schema):
    if schema is None:
        return []
    if not isinstance(schema, list) or len(schema) > MAX_EVIDENCE:
        raise ValueError(f'evidence_schema: a list of up to {MAX_EVIDENCE} fields')
    out, seen = [], set()
    for f in schema:
        if not isinstance(f, dict) or not isinstance(f.get('name'), str) or not NAME.match(f['name']) or f['name'] in seen:
            raise ValueError('evidence_schema: each field needs a unique name (lowercase letters, digits, _)')
        kind = f.get('type', 'text')
        if kind not in EVIDENCE_TYPES:
            raise ValueError(f'evidence_schema {f["name"]}: type must be one of {", ".join(EVIDENCE_TYPES)}')
        item = {'name': f['name'], 'type': kind, 'description': _text(f.get('description'), f'{f["name"]} description', 300)}
        if kind == 'enum':
            options = f.get('options')
            if not isinstance(options, list) or not 2 <= len(options) <= 32 or len(set(options)) != len(options) or \
                    any(not isinstance(o, str) or not o.strip() or len(o) > 100 for o in options):
                raise ValueError(f'evidence_schema {f["name"]}: an enum needs 2-32 unique options')
            item['options'] = options
        if f.get('unit'):
            item['unit'] = _text(f['unit'], f'{f["name"]} unit', 30)
        if f.get('required'):
            item['required'] = True
        seen.add(f['name'])
        out.append(item)
    return out


def _check_type(name, value, spec):
    if value is None or spec is None:
        return
    kind = spec['type']
    ok = {'boolean': isinstance(value, bool),
          'number': isinstance(value, (int, float)) and not isinstance(value, bool),
          'integer': isinstance(value, int) and not isinstance(value, bool) or isinstance(value, float) and value.is_integer(),
          'text': isinstance(value, str),
          'enum': isinstance(value, str) and value in spec.get('options', ()),
          'date': isinstance(value, str) and _is_date(value)}[kind]
    if not ok:
        raise ValueError(f'Evidence {name}: expected {kind}' + (f' ({", ".join(spec["options"])})' if kind == 'enum' else ''))


def _is_date(value):
    try:
        date.fromisoformat(value)
        return len(value) == 10
    except (TypeError, ValueError):
        return False


def validate_evidence(evidence, schema):
    """{name: {'value', 'source', 'status'}} from short ({name: value}) or full entries, checked against the schema."""
    if evidence is None:
        evidence = {}
    if not isinstance(evidence, dict) or len(evidence) > MAX_EVIDENCE:
        raise ValueError(f'evidence: an object with up to {MAX_EVIDENCE} fields')
    specs = {f['name']: f for f in schema}
    out = {}
    for name, entry in evidence.items():
        if not isinstance(name, str) or not NAME.match(name):
            raise ValueError(f'Evidence name {name!r}: lowercase letters, digits and _, starting with a letter')
        if isinstance(entry, dict):
            value = _scalar(entry.get('value'), name)
            source, status = entry.get('source', 'user'), entry.get('status')
            if source not in SOURCES:
                raise ValueError(f'Evidence {name}: source must be one of {", ".join(SOURCES)}')
            status = status or ('unverified' if source == 'extracted' else 'confirmed')
            if status not in STATUSES:
                raise ValueError(f'Evidence {name}: status must be confirmed or unverified')
            check = entry.get('check')
            if check is not None and check not in ('in_text', 'not_in_text', 'inferred', 'not_stated'):
                raise ValueError(f'Evidence {name}: invalid check')
        else:
            value, source, status, check = _scalar(entry, name), 'user', 'confirmed', None
        if specs.get(name, {}).get('type') == 'integer' and isinstance(value, float) and value.is_integer():
            value = int(value)
        _check_type(name, value, specs.get(name))
        out[name] = {'value': value, 'source': source, 'status': status, **({'check': check} if check else {})}
    return out


def validate_tags(tags):
    """Tags as a list of unique lowercase labels (spaces collapsed), up to MAX_TAGS of MAX_TAG_CHARS characters."""
    if tags is None:
        return []
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise ValueError(f'tags: a list of up to {MAX_TAGS} tags')
    out = []
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError('tags: each tag is a text')
        label = ' '.join(tag.split()).lower()
        if not 1 <= len(label) <= MAX_TAG_CHARS or any(ord(c) < 32 for c in label) or any(c in label for c in '<>"\\'):
            raise ValueError(f'Tag {tag!r}: 1-{MAX_TAG_CHARS} characters, without < > " or \\')
        if label not in out:
            out.append(label)
    return out


def validate_title(title):
    """The title the user typed (spaces collapsed), or None when it is missing or empty."""
    if title is None:
        return None
    if not isinstance(title, str):
        raise ValueError('title: a text')
    title = ' '.join(title.split())
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f'title: up to {MAX_TITLE_CHARS} characters')
    return title or None


def _decisions(payload):
    items = payload.get('decisions')
    if items is None and payload.get('decision') is not None:
        items = [payload['decision']]
    if not isinstance(items, list) or not 1 <= len(items) <= 32:
        raise ValueError('Provide a decision or a list of 1-32 decisions')
    questions = {}
    for i, d in enumerate(items, 1):
        if not isinstance(d, dict):
            raise ValueError('Each decision must be an object')
        name = d.get('name') or f'decision_{i}'
        if not isinstance(name, str) or not NAME.match(name) or name in questions:
            raise ValueError(f'Decision {i}: a name, when given, must be unique (lowercase letters, digits and _)')
        label = f'Decision {i}'
        output = d.get('output')
        if output not in OUTPUTS:
            raise ValueError(f'{label}: output must be boolean (Yes / no), choice or score')
        instructions = _text(d.get('instructions'), f'{label} question', 6000, required=True)
        kind = OUTPUTS[output]
        q = {'type': kind, 'instructions': instructions}
        if kind == 'choice':
            alternatives = d.get('alternatives', d.get('criteria'))
            if isinstance(alternatives, list):
                alternatives = {str(a): '' for a in alternatives}
            if not isinstance(alternatives, dict) or len(alternatives) < 2:
                raise ValueError(f'{label}: a choice needs at least two options (returned as: choose when)')
            q['criteria'] = alternatives
            if d.get('abstain_option') is not None:
                q['abstain_option'] = d['abstain_option']
        elif kind == 'noul':
            criteria = d.get('criteria') or {}
            if not isinstance(criteria, dict) or set(criteria) - {'true', 'false'}:
                raise ValueError(f'{label}: Yes / no criteria may only describe "true" and "false"')
            criteria = {k: v.strip() for k, v in criteria.items() if isinstance(v, str) and v.strip()}
            if criteria:
                q['criteria'] = criteria
        else:
            levels = d.get('levels', d.get('criteria'))
            if isinstance(levels, dict):
                numeric = all(re.fullmatch(r'-?\d+(\.\d+)?', str(k)) for k in levels)
                levels = [levels[k] for k in (sorted(levels, key=float) if numeric else levels)]   # numeric keys: by value
            if not isinstance(levels, list):
                raise ValueError(f'{label}: a score needs levels, from lowest to highest')
            q['criteria'] = levels
        questions[name] = q
    return questions


def _attachments(items):
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > MAX_ATTACHMENTS:
        raise ValueError(f'attachments: a list of up to {MAX_ATTACHMENTS} items')
    out = []
    for a in items:
        if not isinstance(a, dict):
            raise ValueError('Each attachment must be an object')
        kind = a.get('type', 'document')
        if kind == 'image':
            raise ValueError('Image attachments are not supported: the local engines read text only')
        if kind not in ('document', 'text'):
            raise ValueError('Attachment type must be document or text')
        content = a.get('content')
        if not isinstance(content, str) or not content.strip():
            raise ValueError('Each attachment needs its text content')
        if len(content) > MAX_ATTACHMENT_CHARS:
            raise ValueError(f'Attachment text is limited to {MAX_ATTACHMENT_CHARS:,} characters (it is never cut automatically)')
        out.append({'type': kind, 'title': _text(a.get('title'), 'Attachment title', 300) or 'Attachment', 'content': content})
    return out


ENGINE_INPUT = 'text'


def _value_text(value):
    return 'true' if value is True else 'false' if value is False else str(value)


def case_text(case):
    """The case as one text: a string, or the earlier {"summary", "details"} object."""
    if isinstance(case, str) and case.strip():
        return _text(case, 'case', 100000)
    if isinstance(case, dict):
        summary = _text(case.get('summary'), 'case.summary', 1000)
        details = _text(case.get('details', case.get('text')), 'case.details', 100000)
        if summary and details and summary not in details:
            return summary + '\n\n' + details
        if summary or details:
            return details or summary
    raise ValueError('case: describe the case (a text)')


def render_state(text, values, attachments, style=ENGINE_INPUT):
    """The state the engines read. text (default): the case as written, one "name: value" line per evidence field in
    name order, then the attachments. json: one canonical object {"case", "evidence", "attachments"}."""
    if style == 'json':
        state = {'case': text, 'evidence': values}
        if attachments:
            state['attachments'] = [{'title': a['title'], 'content': a['content']} for a in attachments]
        return state
    parts = [text.strip()]
    if values:
        parts.append('Evidence:\n' + '\n'.join(f'- {k}: {_value_text(v)}' for k, v in sorted(values.items())))
    for a in attachments:
        parts.append(f'Attachment: {a["title"]}\n{a["content"].strip()}')
    return '\n\n'.join(parts)


def normalize(payload):
    """(internal request with state + questions, case information) for a v2 request."""
    text = case_text(payload.get('case'))
    schema = validate_schema(payload.get('evidence_schema'))
    evidence = validate_evidence(payload.get('evidence'), schema)
    attachments = _attachments(payload.get('attachments'))
    questions = _decisions(payload)
    values = {k: v['value'] for k, v in evidence.items() if v['value'] is not None}
    engine_input = payload.get('engine_input', ENGINE_INPUT)
    if engine_input not in ('text', 'json'):
        raise ValueError('engine_input must be text or json')
    state = render_state(text, values, attachments, engine_input)
    internal = {k: payload[k] for k in PASSTHROUGH if k in payload}
    internal.update(state=state, questions=questions)
    template = payload.get('template')
    if template is not None and (not isinstance(template, str) or len(template) > 100):
        raise ValueError('template: the id of a decision template')
    internal['case_request'] = {'format': 'typelogic-case-v2', 'engine_input': engine_input, 'case': text, 'evidence': evidence,
                                'decision_order': list(questions), 'evidence_schema': schema, 'attachments': attachments,
                                **({'template': template} if template else {})}
    missing = [f['name'] for f in schema if f.get('required') and evidence.get(f['name'], {}).get('value') is None]
    unconfirmed = [k for k, v in evidence.items() if v['status'] == 'unverified']
    return internal, {'missing_required': missing, 'unconfirmed': unconfirmed, 'case': text}


REVIEW_REASONS = {'unconfirmed_evidence': 'Evidence extracted by a model and not confirmed',
                  'missing_required_evidence': 'Required evidence is missing'}


def apply_to_response(response, info):
    """Flag answers whose evidence was extracted and not confirmed, or is missing although required."""
    reasons = []
    if info['unconfirmed']:
        reasons.append('unconfirmed_evidence')
    if info['missing_required']:
        reasons.append('missing_required_evidence')
    for answer in response['answers'].values():
        for reason in reasons:
            if reason not in answer['review_reasons']:
                answer['review_reasons'].append(reason)
        if reasons:
            answer['recommendation'] = 'review'
    response['evidence_review'] = {'unconfirmed': info['unconfirmed'], 'missing_required': info['missing_required']}
    return response
