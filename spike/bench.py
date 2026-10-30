"""Run one GGUF target in llama-server (Vulkan) and answer the suites; writes <out>/<suite>.jsonl like reference_torch.

Decision targets (the specialist converted as a Kev model, Clef-Flash, Kev-4B) answer through POST /v1/systemone.
The Plumb generalist is a text model: it answers with the direct option-letter readout of TypeLogic 1.x
(typelogic/direct.py, the same prompt), the letter log-probabilities coming from POST /completion (n_probs).

    python bench.py --v1 <TypeLogic-AI> --server runtime/llama/llama-server.exe --model models/x.gguf --kind systemone \
        --out results/spec-q8 --suites jevbench,test2,banking77
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import suites  # noqa: E402


def post(url, body, timeout=900):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code}: {exc.read().decode("utf-8", "replace")[:400]}') from None


def get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode('utf-8') or '{}')


class Server:
    """llama-server child process on the loopback address; its log is kept for the memory figures."""

    def __init__(self, exe, model, port, log, device=None, ctx=16384, batch=4096, parallel=2, extra=()):
        self.url = f'http://127.0.0.1:{port}'
        self.log = log
        args = [str(exe), '-m', str(model), '--host', '127.0.0.1', '--port', str(port), '-ngl', '999', '-c', str(ctx),
                '-b', str(batch), '-ub', str(batch), '-np', str(parallel), '--no-webui', '--metrics', '-lv', '4', *extra]
        if device:
            args += ['--device', device]
        self.args = args
        self.handle = open(log, 'w', encoding='utf-8', errors='replace')
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        self.began = time.monotonic()
        self.proc = subprocess.Popen(args, stdout=self.handle, stderr=subprocess.STDOUT, creationflags=flags)

    def wait(self, timeout=900):
        while time.monotonic() - self.began < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f'llama-server exited with {self.proc.returncode}; see {self.log}')
            try:
                status, _ = get(self.url + '/health', 2)
                if status == 200:
                    return time.monotonic() - self.began
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise TimeoutError('llama-server did not become ready')

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.handle.close()


def memory_from_log(path):
    """MiB per buffer kind from the llama.cpp load log (needs -lv 4): device buffers (Vulkan/SYCL/CUDA) and host ones."""
    last = {}
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    for name, kind, mib in re.findall(r'(\S+)\s+(model|KV|RS|compute|output)\s+buffer size\s*=\s*([\d.]+)\s*MiB', text):
        last[(name, kind)] = float(mib)        # a figure logged twice (memory probing) counts once
    out = {'device': {}, 'host': {}}
    for (name, kind), mib in last.items():
        side = 'device' if name.lower().startswith(('vulkan', 'sycl', 'cuda')) else 'host'
        out[side][kind] = round(out[side].get(kind, 0.0) + mib, 1)
    out['total_device_mib'] = round(sum(out['device'].values()), 1)
    out['total_host_mib'] = round(sum(out['host'].values()), 1)
    return out


class DirectReadout:
    """Plumb through llama-server: the 1.x direct prompt, letter log-probabilities from /completion."""

    def __init__(self, v1, tokenizer_dir, url, orders=2, n_probs=64, warm_prefix=False):
        sys.path.insert(0, str(v1))
        from typelogic.direct import DirectEngine
        self.engine = DirectEngine(tokenizer_dir, backend='torch', orders=orders)   # tokenizer only, no weights loaded
        import transformers
        self.engine.tokenizer = transformers.AutoTokenizer.from_pretrained(str(tokenizer_dir), local_files_only=True)
        self.engine.slots = [self.engine._slot(letter) for letter in 'ABCDEFGHIJKLMNOP']
        self.engine.temperature, self.engine.temperature_by_type = 1.0, {}
        self.engine.logits = self.logits
        self.engine.model = 'llama-server'
        self.url, self.n_probs, self.floored = url, n_probs, 0
        self.warm_prefix, self.warmed_tokens = warm_prefix, 0

    def logits(self, ids, count):
        reply = post(self.url + '/completion', {'prompt': ids, 'n_predict': 1, 'n_probs': self.n_probs, 'temperature': 0,
                                                'cache_prompt': True, 'post_sampling_probs': False})
        top = {}
        for alt in reply['completion_probabilities'][0]['top_logprobs']:
            top.setdefault(alt['id'], float(alt['logprob']))
        slots = self.engine.slots[:count]
        known = [top[s] for s in slots if s in top]
        if not known:
            raise RuntimeError('no answer letter among the top log-probabilities')
        floor = min(top.values()) - math.log(1000)
        if len(known) < count:
            self.floored += 1
        return [top.get(s, floor) for s in slots]

    def warm(self, state, questions):
        """Evaluate the prefix shared by every prompt of the request once (n_predict 0). llama-server keeps a context
        checkpoint 4 tokens before the end of each prompt, so the following prompts of a hybrid (recurrent) model
        restart from that state instead of reading the whole evidence again."""
        from typelogic.direct import options_of, view_orders
        prompts = [self.engine.encode(state, q, order)[0] for q in questions
                   for order in view_orders(len(options_of(q)[0]), self.engine.orders)]
        if len(prompts) < 2:
            return
        n = min(len(p) for p in prompts) - 1
        for i in range(n):
            if any(p[i] != prompts[0][i] for p in prompts):
                n = i
                break
        if n >= 64:
            post(self.url + '/completion', {'prompt': prompts[0][:n], 'n_predict': 0, 'cache_prompt': True})
            self.warmed_tokens += n

    def evaluate(self, item):
        from typelogic.core import validate
        state, questions, _ = validate({**suites.payload(item), 'threshold': 0.85})
        if self.warm_prefix and len(questions) > 1:
            # one question: its two option orders already share the checkpoint of the first prompt, an extra
            # warm-up pass only costs time (measured: JevBench p50 0.31 -> 0.52 s with it)
            self.warm(state, questions)
        rows, tokens, floored, _ = self.engine._predict(state, questions)
        return {q['id']: row for q, row in zip(questions, rows)}, tokens, bool(floored)


def answer_suites(server, reader, v1, out_dir, names, banking=1000, limit=None):
    """Answer every item of the suites with one running server; writes out_dir/<suite>.jsonl.
    reader: DirectReadout for a text model, None for a /v1/systemone decision model."""
    for name in names:
        items = suites.load(v1, name, (banking or None) if name == 'banking77' else None)[:limit]
        with (Path(out_dir) / f'{name}.jsonl').open('w', encoding='utf-8') as out:
            for n, item in enumerate(items, 1):
                row = {'id': item['id'], 'suite': name}
                t = time.perf_counter()
                try:
                    if reader:
                        dist, tokens, floored = reader.evaluate(item)
                        row.update(dist=dist, input_tokens=tokens, floored=floored)
                    else:
                        reply = post(server.url + '/v1/systemone', suites.payload(item))
                        row['dist'] = {k: suites.from_systemone(item['questions'][k], reply['answers'][k]) for k in item['questions']}
                        row['input_tokens'] = reply.get('usage', {}).get('input_tokens')
                    row['latency_s'] = time.perf_counter() - t
                except Exception as exc:
                    if server.proc.poll() is not None:     # out of memory or a crash: no result is valid, stop
                        raise RuntimeError(f'llama-server stopped during {name} (exit {server.proc.returncode}); see {server.log}') from exc
                    row.update(error=f'{type(exc).__name__}: {str(exc)[:400]}', latency_s=time.perf_counter() - t)
                out.write(json.dumps(row) + '\n')
                if n % 50 == 0:
                    print(f'  {Path(out_dir).name} {name}: {n}/{len(items)}', flush=True)
        print(f'{Path(out_dir).name} {name}: done', flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--v1', type=Path, required=True)
    p.add_argument('--server', type=Path, required=True)
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--kind', choices=('systemone', 'direct'), required=True)
    p.add_argument('--tokenizer', type=Path, help='direct: tokenizer folder (1.x models/qwen35-4b)')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--suites', default=','.join(suites.SUITES))
    p.add_argument('--limit', type=int, help='smoke test: first N items of every suite')
    p.add_argument('--banking', type=int, default=1000)
    p.add_argument('--device', help='llama.cpp device name, e.g. Vulkan0')
    p.add_argument('--port', type=int, default=18931)
    p.add_argument('--ctx', type=int, default=16384)
    p.add_argument('--batch', type=int, default=4096)
    p.add_argument('--parallel', type=int, default=2)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    server = Server(args.server, args.model, args.port, args.out / 'server.log', args.device, args.ctx, args.batch, args.parallel)
    try:
        load = server.wait()
        info = {'model': args.model.name, 'bytes': args.model.stat().st_size, 'kind': args.kind, 'load_seconds': round(load, 2),
                'args': server.args[1:], 'memory': memory_from_log(server.log)}
        try:
            info['props'] = get(server.url + '/props', 10)[1]
            info['props'].pop('chat_template', None)
        except (OSError, ValueError):
            pass
        reader = DirectReadout(args.v1, args.tokenizer, server.url) if args.kind == 'direct' else None
        answer_suites(server, reader, args.v1, args.out, [s for s in args.suites.split(',') if s], args.banking, args.limit)
        info['memory'] = memory_from_log(server.log)
        (args.out / 'target.json').write_text(json.dumps(info, indent=2, default=str), encoding='utf-8')
    finally:
        server.stop()


if __name__ == '__main__':
    main()
