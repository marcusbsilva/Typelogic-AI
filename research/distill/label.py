"""Label generated questions with several OpenRouter teachers (step 2): soft distributions over the options."""
import concurrent.futures as cf
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(os.environ.get('TYPELOGIC_DISTILL_WORK', ROOT / 'research/distill/work'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from orclient import chat, spent

from typelogic.core import validate
from typelogic.direct import LETTERS, messages, options_of

RAW = (WORK / 'raw/generated.jsonl')
OUT = (WORK / 'raw/labels.jsonl')
BUDGET = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
TEACHERS = [t for t in (sys.argv[2].split(',') if len(sys.argv) > 2 else
            ['qwen/qwen3.6-35b-a3b', 'qwen/qwen3.8-flash', 'deepseek/deepseek-v4.1-flash', 'google/gemma-4-26b-a4b-it'])]
LOGPROB_TEACHERS = {'qwen/qwen3.6-35b-a3b', 'qwen/qwen3.8-flash'}


def distribution(out, count):
    choice = out['choices'][0]
    letters = LETTERS[:count]
    lp = choice.get('logprobs') or {}
    content = lp.get('content') or []
    if content:
        top = {}
        for alt in content[0].get('top_logprobs') or []:
            token = alt['token'].strip()
            if token in letters and token not in top:
                top[token] = alt['logprob']
        if top:
            floor = min(top.values()) - math.log(1000)
            z = [top.get(l, floor) for l in letters]
            m = max(z)
            e = [math.exp(v - m) for v in z]
            s = sum(e)
            return [v / s for v in e], 'logprobs'
    text = (choice['message'].get('content') or '').strip().strip('"').upper()[:1]
    if text in letters:
        return [1.0 if l == text else 0.0 for l in letters], 'letter'
    return None, 'unparsed'


def label(row, qid, spec, teacher):
    state, questions, _ = validate({'state': row['state'], 'questions': {qid: spec}})
    q = questions[0]
    option_ids, descriptions = options_of(q)
    kw = dict(max_tokens=1, temperature=0, reasoning={'enabled': False})
    if teacher in LOGPROB_TEACHERS:
        kw.update(logprobs=True, top_logprobs=5)
    out = chat(teacher, messages(state, q['instr'], descriptions), BUDGET, **kw)
    dist, how = distribution(out, len(option_ids))
    return {'id': row['id'], 'qid': qid, 'teacher': teacher, 'returned': out.get('model'), 'option_ids': option_ids,
            'distribution': dist, 'how': how, 'cost': (out.get('usage') or {}).get('cost')}


def main():
    done = set()
    if OUT.exists():
        for line in OUT.read_text().splitlines():
            r = json.loads(line); done.add((r['id'], r['qid'], r['teacher']))
    jobs = []
    for line in RAW.read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        for qid, spec in row['questions'].items():
            for teacher in TEACHERS:
                if (row['id'], qid, teacher) not in done:
                    jobs.append((row, qid, spec, teacher))
    print(len(jobs), 'calls to make', flush=True)
    ok = bad = 0
    started = time.time()
    with cf.ThreadPoolExecutor(12) as pool, OUT.open('a', encoding='utf-8') as f:
        futures = [pool.submit(label, *job) for job in jobs]
        for fut in cf.as_completed(futures):
            try:
                rec = fut.result()
                ok += 1; f.write(json.dumps(rec, ensure_ascii=False) + '\n'); f.flush()
            except Exception as exc:
                bad += 1
                if 'budget exhausted' in str(exc):
                    print('budget exhausted; stopping'); pool.shutdown(cancel_futures=True); break
            if (ok + bad) % 200 == 0:
                print(f'{ok} ok, {bad} failed, spent ${spent():.3f}, {time.time() - started:.0f}s', flush=True)
    print(f'done: {ok} ok, {bad} failed, total spent ${spent():.3f}')


if __name__ == '__main__':
    main()
