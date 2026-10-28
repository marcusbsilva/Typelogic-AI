"""Field extraction with Needle 3, offline.

Needle 3 (Cactus Compute, Apache-2.0, https://github.com/cactus-compute/needle) is a 121M-parameter tool-calling model.
TypeLogic uses it for one optional job: reading a scenario and filling fields the user declares (amounts, codes, dates,
actions). The values are never trusted: each one is checked against the scenario text here, and a decision that
includes them always requires review.

Only the native engine (libneedle3.dll / .so / .dylib) and the weights (needle3.cact) are used, from models/needle/,
after their SHA-256 hashes are checked against models/needle/manifest.json. The `cactus-needle` Python package is not
imported: it sends usage telemetry and contacts Hugging Face whenever it loads a model. The native engine has no
network code (it imports no socket or HTTP functions). The ctypes signatures below are adapted from needle/__init__.py
of cactus-compute/needle (Apache License 2.0, see licenses/needle-Apache-2.0.txt); the rest is TypeLogic code.

The engine keeps one global state per process, so every call goes through one lock.
"""
import ctypes
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time

# The package is never imported, but the variables also cover any copy of it a user may have installed.
os.environ['NEEDLE_TELEMETRY'] = '0'
os.environ['DO_NOT_TRACK'] = '1'

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = 'needle3.cact'
LIBRARIES = {'win32': 'libneedle3.dll', 'darwin': 'libneedle3.dylib'}
MAX_TEXT_CHARS = 20000      # longer text is refused, never cut (extraction takes about 1 s per 1,500 characters)
MAX_FIELDS = 12
MAX_OPTIONS = 20
MAX_NEW_TOKENS = 512
FIELD_TYPES = ('text', 'number', 'integer', 'date', 'boolean')
NAME = re.compile(r'^[a-z][a-z0-9_]{0,39}$')
TOOL_NAME = 'record_fields'
TOOL_DESCRIPTION = 'Record the requested fields exactly as stated in the text. Leave out any field the text does not state.'
NOTE = ('Extracted by a small local model (Needle 3). The values are unverified: the scenario text is authoritative, '
        'and a decision that includes them requires review.')

PRESETS = {
    'request': {'label': 'Trusted record vs received request', 'fields': [
        {'name': 'trusted_amount', 'type': 'number', 'description': 'Amount in the trusted server record'},
        {'name': 'received_amount', 'type': 'number', 'description': 'Amount in the received request'},
        {'name': 'received_action', 'type': 'text', 'description': 'Action in the received request'},
        {'name': 'added_role', 'type': 'text', 'description': 'Role or permission added by the received request'}]},
    'invoice': {'label': 'Invoice or quote', 'fields': [
        {'name': 'vendor', 'type': 'text', 'description': 'Vendor or supplier name'},
        {'name': 'document_number', 'type': 'text', 'description': 'Invoice or quote number'},
        {'name': 'amount', 'type': 'number', 'description': 'Total amount'},
        {'name': 'currency', 'type': 'text', 'description': 'Currency code'},
        {'name': 'due_date', 'type': 'date', 'description': 'Due date'}]},
    'payment': {'label': 'Payment or refund request', 'fields': [
        {'name': 'amount', 'type': 'number', 'description': 'Amount paid'},
        {'name': 'payment_date', 'type': 'date', 'description': 'Date of the payment'},
        {'name': 'requested_action', 'type': 'text', 'description': 'What the customer asks for'}]},
    'stock': {'label': 'Stock request', 'fields': [
        {'name': 'item_code', 'type': 'text', 'description': 'Item code'},
        {'name': 'units_in_stock', 'type': 'integer', 'description': 'Units in stock'},
        {'name': 'units_needed', 'type': 'integer', 'description': 'Units needed'}]},
}

_MONTHS = [('january', 'jan', 'janeiro', 'enero'), ('february', 'feb', 'fevereiro', 'febrero', 'fev'),
           ('march', 'mar', 'março', 'marco', 'marzo'), ('april', 'apr', 'abril', 'abr'), ('may', 'maio', 'mayo', 'mai'),
           ('june', 'jun', 'junho', 'junio'), ('july', 'jul', 'julho', 'julio'), ('august', 'aug', 'agosto', 'ago'),
           ('september', 'sep', 'sept', 'setembro', 'septiembre', 'set'), ('october', 'oct', 'outubro', 'octubre', 'out'),
           ('november', 'nov', 'novembro', 'noviembre'), ('december', 'dec', 'dezembro', 'diciembre', 'dez', 'dic')]


def folder():
    value = os.environ.get('TYPELOGIC_NEEDLE_DIR', '').strip()
    return Path(value) if value else ROOT / 'models' / 'needle'


def library_name():
    return LIBRARIES.get(sys.platform, 'libneedle3.so')


def manifest(path=None):
    return json.loads(((path or folder()) / 'manifest.json').read_text(encoding='utf-8'))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------- field specification and evidence checks

def validate_fields(fields):
    """Checked copy of a field list: [{name, type, description?, options?}]."""
    if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
        raise ValueError(f'Declare 1-{MAX_FIELDS} fields')
    out, seen = [], set()
    for item in fields:
        if not isinstance(item, dict):
            raise ValueError('Each field must be an object with name and type')
        name, kind = item.get('name'), item.get('type', 'text')
        if not isinstance(name, str) or not NAME.match(name):
            raise ValueError('Field names: lowercase letters, digits and _, starting with a letter, up to 40 characters')
        if name in seen:
            raise ValueError(f'Duplicate field: {name}')
        if kind not in FIELD_TYPES:
            raise ValueError(f'Field {name}: type must be one of {", ".join(FIELD_TYPES)}')
        description = item.get('description') or ''
        if not isinstance(description, str) or len(description) > 300:
            raise ValueError(f'Field {name}: description up to 300 characters')
        options = item.get('options')
        if options is not None:
            if kind != 'text' or not isinstance(options, list) or not 2 <= len(options) <= MAX_OPTIONS or \
                    any(not isinstance(o, str) or not o.strip() or len(o) > 100 for o in options) or len(set(options)) != len(options):
                raise ValueError(f'Field {name}: options must be 2-{MAX_OPTIONS} unique texts (text fields only)')
        seen.add(name)
        out.append({'name': name, 'type': kind, 'description': description.strip(), **({'options': options} if options else {})})
    return out


def tool_schema(fields):
    properties = {}
    for f in fields:
        kind = {'text': 'string', 'date': 'string'}.get(f['type'], f['type'])
        spec = {'type': kind, 'description': f['description'] or f['name'].replace('_', ' ')}
        if f['type'] == 'date':
            spec['format'] = 'date'
        if f.get('options'):
            spec['enum'] = f['options']
        properties[f['name']] = spec
    # No required fields: the engine then leaves out what the text does not state instead of inventing a value.
    return {'name': TOOL_NAME, 'description': TOOL_DESCRIPTION,
            'parameters': {'type': 'object', 'properties': properties, 'required': []}}


_NUMBER = re.compile(r'(?<![\w.,])[-+]?\d[\d.,   ]*\d(?![\w])|(?<![\w.,])[-+]?\d(?![\w])')


_NUMBER_WORDS = {w: i for i, group in enumerate([
    ('zero', 'nenhum', 'nenhuma', 'cero'), ('one',), ('two', 'dois', 'duas'),
    ('three', 'três', 'tres'), ('four', 'quatro', 'cuatro'), ('five', 'cinco'), ('six', 'seis'), ('seven', 'sete', 'siete'),
    ('eight', 'oito', 'ocho'), ('nine', 'nove', 'nueve'), ('ten', 'dez', 'diez'), ('eleven', 'onze', 'once'), ('twelve', 'doze', 'doce')]) for w in group}
_COUNTED = re.compile(r'\b(' + '|'.join(sorted(map(re.escape, _NUMBER_WORDS), key=len, reverse=True)) + r')\s+([^\W\d_]+)', re.I)


def _numbers(text):
    """Every number written in `text`: digits with dot or comma decimals and thousands separators, and small numbers
    written as words before a noun ("zero units", "one unit", "duas vezes"). Articles such as "a" or "uma" are not
    counted, so a value of 1 written only that way is reported as not written in the text."""
    values = {float(_NUMBER_WORDS[m.group(1).lower()]) for m in _COUNTED.finditer(text)}
    for match in _NUMBER.finditer(text):
        raw = re.sub(r'[   ]', '', match.group(0)).rstrip('.,')
        candidates = {raw}
        if ',' in raw and '.' in raw:
            candidates = {raw.replace(',', '')} if raw.rfind('.') > raw.rfind(',') else {raw.replace('.', '').replace(',', '.')}
        elif ',' in raw:
            candidates = {raw.replace(',', ''), raw.replace(',', '.')} if raw.count(',') == 1 else {raw.replace(',', '')}
        elif raw.count('.') > 1:
            candidates = {raw.replace('.', '')}
        elif '.' in raw:
            candidates = {raw, raw.replace('.', '')}
        for c in candidates:
            try:
                values.add(float(c))
            except ValueError:
                pass
    return values


def _norm(text):
    return ' '.join(str(text).casefold().split())


def _date_in_text(value, text):
    """True when the date is written in the text: ISO, day/month/year in digits, or day, month name and year."""
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    if value in text:
        return True
    low = _norm(text)
    year = rf'(\d\d)?{day.year % 100:02d}'
    for first, second in ((day.day, day.month), (day.month, day.day)):
        if re.search(rf'(?<!\d)0?{first}[/.\-]0?{second}[/.\-]{year}(?!\d)', low):
            return True
    words = set(re.findall(r'[^\W\d_]+', low))
    named = any(name in words for name in _MONTHS[day.month - 1])
    written_day = re.search(rf'(?<!\d)0?{day.day}(st|nd|rd|th|º)?(?!\d)', low) is not None
    return named and written_day and re.search(rf'(?<!\d){day.year}(?!\d)', low) is not None


def evidence(value, kind, text):
    """'in_text' when the value is written in the text, 'not_in_text' when it is not, 'inferred' for yes/no fields."""
    if kind == 'boolean':
        return 'inferred'
    if kind in ('number', 'integer'):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return 'not_in_text'
        return 'in_text' if any(abs(v - value) <= 1e-9 * max(1.0, abs(value)) for v in _numbers(text)) else 'not_in_text'
    if kind == 'date':
        return 'in_text' if _date_in_text(value, text) else 'not_in_text'
    return 'in_text' if isinstance(value, str) and value.strip() and _norm(value) in _norm(text) else 'not_in_text'


def _typed(value, kind):
    if value is None:
        return False
    if kind == 'boolean':
        return isinstance(value, bool)
    if kind == 'integer':
        return isinstance(value, int) and not isinstance(value, bool) or isinstance(value, float) and value.is_integer()
    if kind == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return isinstance(value, str)


def check_attached(record):
    """Validate the `extracted_fields` object a decision request may carry (as returned by extract())."""
    if not isinstance(record, dict) or record.get('engine') != 'needle3' or not isinstance(record.get('fields'), list) \
            or not 1 <= len(record['fields']) <= MAX_FIELDS:
        raise ValueError('extracted_fields must be the object returned by field extraction (engine needle3, 1-12 fields)')
    fields = []
    for f in record['fields']:
        if not isinstance(f, dict) or not isinstance(f.get('name'), str) or not NAME.match(f['name']) or \
                f.get('type') not in FIELD_TYPES or f.get('check') not in ('in_text', 'not_in_text', 'inferred', 'not_stated'):
            raise ValueError('extracted_fields: invalid field entry')
        value = f.get('value')
        if value is not None and not _typed(value, f['type']) or isinstance(value, str) and len(value) > 1000:
            raise ValueError(f'extracted_fields: invalid value for {f["name"]}')
        fields.append({'name': f['name'], 'type': f['type'], 'value': value, 'check': f['check']})
    return {'engine': 'needle3', 'fields': fields, 'values_are_unverified': True, 'note': NOTE}


# ---------------------------------------------------------------- native engine

class Runtime:
    """The native Needle 3 engine, loaded on first use from verified local files."""

    def __init__(self, path=None, loader=None):
        self.path = Path(path) if path else folder()
        self.loader = loader or ctypes.CDLL
        self.lock = threading.Lock()
        self.lib = None
        self.error = None
        self.tools = None

    def files(self):
        return {'library': self.path / library_name(), 'weights': self.path / WEIGHTS, 'license': self.path / 'LICENSE'}

    def status(self):
        files = self.files()
        missing = [k for k in ('library', 'weights') if not files[k].is_file()]
        info = {'engine': 'needle3', 'folder': str(self.path), 'loaded': self.lib is not None,
                'limits': {'max_text_chars': MAX_TEXT_CHARS, 'max_fields': MAX_FIELDS},
                'types': list(FIELD_TYPES), 'presets': PRESETS, 'network': 'none (native engine only; the needle Python package is not used)'}
        if missing:
            info.update(available=False, reason='Needle is not installed (run: TypeLogic.bat models --only needle)')
        elif self.error:
            info.update(available=False, reason=self.error)
        else:
            info.update(available=True, reason=None)
        return info

    def _verify(self):
        record = manifest(self.path)
        files = self.files()
        if sha256(files['weights']) != record['weights']['sha256']:
            raise RuntimeError(f'{WEIGHTS} does not match models/needle/manifest.json (run: TypeLogic.bat models --only needle)')
        known = {p['sha256'] for p in record['platforms'].values()}
        if sha256(files['library']) not in known:
            raise RuntimeError(f'{files["library"].name} does not match models/needle/manifest.json (run: TypeLogic.bat models --only needle)')

    def _load(self):
        if self.lib is not None:
            return self.lib
        try:
            self._verify()
            lib = self.loader(str(self.files()['library']))
            lib.needle_init.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
            lib.needle_init.restype = ctypes.c_int
            lib.needle_complete.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.c_int,
                                            ctypes.c_char_p, ctypes.c_int]
            lib.needle_complete.restype = ctypes.c_int
            lib.needle_reset.argtypes = []
            lib.needle_reset.restype = None
            lib.needle_load.argtypes = [ctypes.c_char_p, ctypes.c_uint64]
            lib.needle_load.restype = ctypes.c_int
            data = self.files()['weights'].read_bytes()
            if lib.needle_load(data, len(data)) != 0:
                raise RuntimeError('The Needle engine could not read needle3.cact')
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            self.error = str(exc)[:300]
            raise RuntimeError(self.error) from exc
        self.lib = lib
        return lib

    def _complete(self, lib, system, tools, text):
        if self.tools != tools:
            if lib.needle_init(system.encode(), tools.encode(), None) < 0:
                raise RuntimeError('The Needle engine rejected the field list')
            self.tools = tools
        else:
            lib.needle_reset()
        size = 1 << 16
        while True:
            buffer = ctypes.create_string_buffer(size)
            rc = lib.needle_complete(text.encode('utf-8'), None, 0, MAX_NEW_TOKENS, buffer, size)
            raw = buffer.value.decode('utf-8', 'replace')
            if rc < 0 and not raw and size < 1 << 22:
                size *= 4     # the output did not fit the buffer
                lib.needle_reset()
                continue
            break
        try:
            result = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError('The Needle engine returned invalid output') from exc
        lib.needle_reset()
        if rc < 0 or result.get('type') == 'error':
            self.tools = None
            raise RuntimeError('Needle: ' + str(result.get('error') or f'error code {rc}')[:200])
        return result

    def extract(self, text, fields, today=None):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Provide the scenario text to read')
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f'Field extraction reads up to {MAX_TEXT_CHARS:,} characters; this text has {len(text):,}. '
                             'Shorten it or extract from a part of it; the text is never cut automatically.')
        fields = validate_fields(fields)
        today = today or date.today()
        system = f'date: {today.isoformat()} {today.strftime("%a")}'   # resolves relative dates; they are then marked not_in_text
        tools = json.dumps([tool_schema(fields)], ensure_ascii=False)
        started = time.perf_counter()
        with self.lock:
            lib = self._load()
            result = self._complete(lib, system, tools, text)
        seconds = time.perf_counter() - started
        calls = result.get('function_calls') or []
        withheld = not calls and bool(result.get('suppressed_calls'))
        call = next((c for c in (calls or result.get('suppressed_calls') or []) if c.get('name') == TOOL_NAME), {})
        arguments = call.get('arguments') if isinstance(call.get('arguments'), dict) else {}
        out = []
        for f in fields:
            value = arguments.get(f['name'])
            if f['type'] == 'integer' and isinstance(value, float) and value.is_integer():
                value = int(value)
            if not _typed(value, f['type']) or f.get('options') and value not in f['options']:
                value = None
            if isinstance(value, str) and not value.strip():
                value = None
            out.append({'name': f['name'], 'type': f['type'], 'value': value,
                        'check': 'not_stated' if value is None else evidence(value, f['type'], text)})
        confidence = result.get('confidence')
        return {'engine': 'needle3', 'fields': out, 'chars': len(text), 'seconds': round(seconds, 3),
                'confidence': round(confidence, 4) if isinstance(confidence, (int, float)) else None,
                'withheld_by_engine': withheld, 'reasoning': str(result.get('reasoning') or '')[:2000],
                'engine_flags': result.get('validation') or {}, 'note': NOTE}


_runtime = None
_runtime_lock = threading.Lock()


def runtime():
    global _runtime
    with _runtime_lock:
        if _runtime is None or _runtime.path != folder():
            _runtime = Runtime()
        return _runtime


def status():
    return runtime().status()


def extract(text, fields, today=None):
    return runtime().extract(text, fields, today)
