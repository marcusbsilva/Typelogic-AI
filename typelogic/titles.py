"""Short case titles: the title the user typed, else one generated from the case after the evaluation.

Generation runs on this computer only. When Ollama already runs with one of the given models pulled, that model
writes a title of a few words (JSON output, temperature 0); otherwise, or when its answer is unusable, the title is
taken from the first sentence of the case. Ollama is never started or asked to download anything for a title.
"""
import json
import re
import urllib.request

from . import ollama

MAX_WORDS = 8
MAX_CHARS = 80
SYSTEM = ('Write a short title for the case the user sends: 3 to 7 words, in the language of the case, naming what the '
          'case is about. No quotes, no final period, no decision or verdict.')
_SENTENCE = re.compile(r'(?<=[.!?])\s+|\n+')
# Words that should not end a shortened title ("... three days in a" -> "... three days").
_TRAILING = {'a', 'an', 'the', 'of', 'in', 'on', 'at', 'to', 'for', 'from', 'by', 'with', 'and', 'or', 'but', 'my', 'your',
             'our', 'their', 'his', 'her', 'its', 'is', 'are', 'was', 'were', 'be', 'that', 'which', 'who', 'as', 'than'}
_GREETING = re.compile(r'^(hi|hello|hey|dear|good (morning|afternoon|evening))\b[^,.!?]*[,.!?]?\s*', re.I)


def clean(value):
    """A usable title (one line, trimmed, at most MAX_CHARS) or None."""
    if not isinstance(value, str):
        return None
    title = ' '.join(value.split()).strip(' "\'`*#-:').rstrip('.')
    if not title or len(title) > MAX_CHARS:
        return None
    return title[0].upper() + title[1:]


def from_text(text):
    """The first sentence of the case with at least three words, cut to MAX_WORDS words."""
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    sentences = [s.strip() for s in _SENTENCE.split(text) if s.strip()]
    chosen = next((s for s in sentences if len(s.split()) >= 3), sentences[0] if sentences else 'Decision')
    chosen = _GREETING.sub('', chosen).strip() or chosen
    words = chosen.split()
    if len(words) > MAX_WORDS:
        words = words[:MAX_WORDS]
        while len(words) > 3 and words[-1].lower().strip(',;:') in _TRAILING:
            words.pop()
    title = ' '.join(words).rstrip(',;:')
    if len(title) > MAX_CHARS:
        title = title[:MAX_CHARS].rsplit(' ', 1)[0]
    return clean(title) or 'Decision'


def _ask(model, text, timeout):
    body = {'model': model, 'stream': False, 'think': False, 'keep_alive': '10m',
            'format': {'type': 'object', 'properties': {'title': {'type': 'string'}}, 'required': ['title']},
            'options': {'temperature': 0, 'num_predict': 40, 'num_ctx': 8192},
            'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': text[:6000]}]}
    request = urllib.request.Request(ollama.url() + '/api/chat', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        reply = json.loads(response.read().decode('utf-8'))
    return clean(json.loads(reply['message']['content']).get('title'))


def generate(text, models=(), timeout=60):
    """(title, source): source is 'model:<name>' or 'case text'."""
    names = ollama.tags() if models else None
    for model in models:
        if names is None or not model or not ollama.has_model(names, model):
            continue
        try:
            title = _ask(model, text if isinstance(text, str) else json.dumps(text, ensure_ascii=False), timeout)
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            title = None
        if title and len(title.split()) <= MAX_WORDS + 4:
            return title, f'model:{model}'
    return from_text(text), 'case text'
