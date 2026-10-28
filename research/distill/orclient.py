"""Minimal OpenRouter client with a hard spending guard. The key comes only from the OPENROUTER_API_KEY environment variable and is never printed or written."""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(os.environ.get('TYPELOGIC_DISTILL_WORK', ROOT / 'research/distill/work'))
BASE = 'https://openrouter.ai/api/v1'
LEDGER = WORK / 'ledger.jsonl'
_lock = threading.Lock()


def _key():
    key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if not key:
        raise SystemExit('Set OPENROUTER_API_KEY in your shell (never commit it).')
    return key

def _req(path, payload=None, timeout=120):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={'Authorization': 'Bearer ' + _key(), 'Content-Type': 'application/json',
                                                               'X-Title': 'TypeLogic dataset builder'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def balance():
    d = _req('/credits')['data']
    return d['total_credits'] - d['total_usage']

def spent():
    total = 0.0
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            total += json.loads(line).get('cost', 0) or 0
    return total

def chat(model, messages, budget_left, **kw):
    """One chat call; refuses when the local ledger says the budget is exhausted."""
    if spent() >= budget_left:
        raise RuntimeError('budget exhausted')
    payload = {'model': model, 'messages': messages, 'stream': False, 'usage': {'include': True}}
    payload.update(kw)
    for attempt in range(3):
        try:
            out = _req('/chat/completions', payload)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:300]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(3 * (attempt + 1)); continue
            raise RuntimeError(f'HTTP {e.code}: {body}')
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(3); continue
            raise
    usage = out.get('usage') or {}
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with _lock, LEDGER.open('a') as f:
        f.write(json.dumps({'t': time.time(), 'model': model, 'returned': out.get('model'), 'cost': usage.get('cost', 0),
                            'in': usage.get('prompt_tokens'), 'out': usage.get('completion_tokens')}) + '\n')
    return out
