"""Generate typed-decision documents + questions with OpenRouter teachers (step 1 of the TypeLogic distillation set)."""
import concurrent.futures as cf
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(os.environ.get('TYPELOGIC_DISTILL_WORK', ROOT / 'research/distill/work'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from orclient import chat, spent

from typelogic.schema import validate

OUT = (WORK / 'raw/generated.jsonl')
BUDGET = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0      # USD for this step (ledger total cap)
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 50
GENERATORS = ['deepseek/deepseek-v4.1-flash', 'qwen/qwen3.8-flash']

DOMAINS = [
    'retail refund and return policy', 'airline delay compensation rules', 'home insurance claim with exclusions', 'health insurance prior authorization',
    'employee leave and overtime policy', 'IT incident severity triage', 'cloud access request approval', 'security alert investigation',
    'procurement purchase approval thresholds', 'invoice matching and payment release', 'expense reimbursement policy', 'loan application pre-screening',
    'bank card dispute handling', 'KYC document verification', 'shipping and customs clearance', 'warehouse inventory discrepancy',
    'SaaS subscription billing support ticket', 'software bug report triage', 'code review merge policy', 'data retention and deletion request',
    'GDPR / LGPD data subject request', 'rental contract and deposit rules', 'school enrollment eligibility', 'scholarship eligibility rules',
    'clinic appointment scheduling rules', 'pharmacy prescription refill policy', 'restaurant reservation and cancellation policy', 'hotel booking change rules',
    'event ticket transfer policy', 'vehicle warranty claim', 'utility bill dispute', 'telecom plan change request', 'customer support chat routing',
    'content moderation guideline', 'marketing email consent rules', 'manufacturing quality hold and release', 'food safety temperature log review',
    'construction permit checklist', 'agent tool-use safety gate (should the AI agent run this action)', 'meeting scheduling across time zones',
    'tax deduction eligibility (simplified fictional rules)', 'grant reporting deadline compliance', 'open-source license compatibility check',
]
SKILLS = ['multi-hop reasoning across two or more clauses', 'dates and deadlines arithmetic', 'numeric thresholds and sums', 'an exception that overrides a general rule',
          'negation and double negatives', 'a distractor detail that looks relevant but is not', 'missing information where the right answer is insufficient evidence',
          'conflicting statements where the more specific or more recent one wins', 'ordinal severity or priority grading', 'picking the single best routing destination']

PROMPT = """You write evaluation data for a model that makes typed decisions from a document.
Write ONE self-contained, realistic, fictional case in {language} about: {domain}.
Requirements:
- "state": a document of {words} words (policy excerpt + case facts, an email thread, a ticket, a log or a form). Use concrete names, dates, amounts. Everything needed to answer must be in it (or deliberately missing when the answer is "insufficient information").
- 3 or 4 questions that require careful reading; at least one must exercise: {skill}. Mix the types:
  * "choice": 3 to 8 options as an object {{"snake_case_key": "short description"}}; include an "insufficient_information" key only when it is plausible.
  * "noul": a yes/no proposition; give "criteria" {{"true": "...", "false": "..."}}.
  * "score": an ordinal scale, "criteria" is a list of 3 to 5 level descriptions from lowest to highest.
- Exactly one option is clearly correct for a careful expert; the others must be plausible.
- Never reveal the answer inside the state or the question text.
Return only JSON (no markdown):
{{"language": "{lang_code}", "domain": "...", "state": "...", "questions": {{"q1": {{"type": "...", "instructions": "...", "criteria": ..., "answer": "<key | true | false | level index as string>", "rationale": "<one sentence>", "skill": "..."}}, ...}}}}"""


def parse(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    start, end = text.find('{'), text.rfind('}')
    return json.loads(text[start:end + 1])


def clean(item):
    """Keep only well-formed questions; returns the TypeLogic payload and author answers."""
    questions, answers, meta = {}, {}, {}
    for qid, q in (item.get('questions') or {}).items():
        try:
            spec = {'type': q['type'], 'instructions': q['instructions']}
            if q['type'] in ('choice', 'score'):
                spec['criteria'] = q['criteria']
            elif q.get('criteria'):
                spec['criteria'] = {k: v for k, v in q['criteria'].items() if k in ('true', 'false')}
            validate({'state': item['state'], 'questions': {qid: spec}})
            answer = str(q['answer']).strip().lower() if q['type'] == 'noul' else str(q['answer']).strip()
            keys = list(spec['criteria']) if q['type'] == 'choice' else [str(i) for i in range(len(spec['criteria']))] if q['type'] == 'score' else ['true', 'false']
            if answer not in keys or (q['type'] == 'choice' and not 3 <= len(keys) <= 16):
                continue
            questions[qid], answers[qid] = spec, answer
            meta[qid] = {'skill': q.get('skill'), 'rationale': q.get('rationale')}
        except Exception:
            continue
    return questions, answers, meta


def one(i, seed):
    rng = random.Random(seed)
    lang = 'pt-BR' if rng.random() < 0.3 else 'en'
    prompt = PROMPT.format(language='Brazilian Portuguese' if lang == 'pt-BR' else 'English', lang_code=lang, domain=rng.choice(DOMAINS),
                           words=rng.choice([180, 300, 450, 700, 1000]), skill=rng.choice(SKILLS))
    model = GENERATORS[i % len(GENERATORS)]
    out = chat(model, [{'role': 'user', 'content': prompt}], BUDGET, max_tokens=3500, temperature=0.9, reasoning={'enabled': False})
    item = parse(out['choices'][0]['message']['content'])
    questions, answers, meta = clean(item)
    if len(questions) < 2:
        return None
    state = item['state']
    return {'id': 'tl-distill-' + hashlib.sha256((state + str(seed)).encode()).hexdigest()[:16], 'language': lang, 'domain': item.get('domain'),
            'generator': out.get('model') or model, 'state': state, 'questions': questions, 'author_answers': answers, 'meta': meta,
            'cost': (out.get('usage') or {}).get('cost')}


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    done = sum(1 for _ in OUT.open()) if OUT.exists() else 0
    seeds = [random.Random(2026 + done + k).randrange(1 << 30) for k in range(COUNT)]
    ok = bad = 0
    started = time.time()
    with cf.ThreadPoolExecutor(16) as pool, OUT.open('a', encoding='utf-8') as f:
        futures = [pool.submit(one, done + k, s) for k, s in enumerate(seeds)]
        for fut in cf.as_completed(futures):
            try:
                row = fut.result()
            except Exception as exc:
                bad += 1
                if 'budget exhausted' in str(exc):
                    print('budget exhausted; stopping'); pool.shutdown(cancel_futures=True); break
                continue
            if row:
                ok += 1; f.write(json.dumps(row, ensure_ascii=False) + '\n'); f.flush()
            else:
                bad += 1
            if (ok + bad) % 25 == 0:
                print(f'{ok} ok, {bad} rejected, spent ${spent():.3f}, {time.time() - started:.0f}s', flush=True)
    print(f'done: {ok} ok, {bad} rejected, total spent ${spent():.3f}')


if __name__ == '__main__':
    main()
