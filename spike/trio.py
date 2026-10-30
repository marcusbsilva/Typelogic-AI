"""All three models on the GPU at once: specialist (Kev, patched llama.dll), Plumb (direct readout) and Clef-Flash.

Memory plan (measured alone: 2.6 + 3.6 + 6.9 GiB = 13.1 GiB > 11.2 GiB free on a 12 GB B580):
  * the tied output matrix (token_embd, n_vocab x n_embd) stays in system memory for every model (-ot): the
    specialist and Clef-Flash never use the LM head, Plumb uses it for one token per request;
  * Plumb: one slot, 8192-token context, 8-bit KV cache (flash attention), 512-token micro-batch, prompt cache in
    RAM capped at 1 GiB; the prefix shared by the prompts of a request is evaluated once (context checkpoint);
  * specialist: 12288-token context, 3072-token micro-batch (a question with its options fits in one micro-batch);
  * Clef-Flash: one slot, 4096-token micro-batch (the whole request must fit; longest JevBench request: 4032 tokens).
The servers start largest first; free device memory is read with --list-devices while all three are loaded. Then
every suite is answered by its server while the others stay loaded, and a stress phase sends requests to the three
servers at the same time. Results go to results/trio-<model> (read by report.py) and results/trio-<clef>.json.

    python trio.py --v1 <TypeLogic-AI> --clef clef-flash-iq4xs-b --device Vulkan0
"""
import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import suites  # noqa: E402
from bench import DirectReadout, Server, answer_suites, memory_from_log, post  # noqa: E402

V2 = HERE.parent
LOCK = json.loads((V2 / 'config' / 'models.lock.json').read_text(encoding='utf-8'))
OUTPUT_ON_CPU = ['-ot', r'token_embd\.weight=CPU']


def free_mib(server_exe, device):
    out = subprocess.run([str(server_exe), '--list-devices'], capture_output=True, text=True, timeout=120)
    for line in (out.stdout + out.stderr).splitlines():
        m = re.match(r'\s*(\w+\d+)\s*:.*\((\d+) MiB, (\d+) MiB free\)', line)
        if m and (device is None or m.group(1) == device):
            return {'device': m.group(1), 'total_mib': int(m.group(2)), 'free_mib': int(m.group(3))}
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--v1', type=Path, required=True)
    p.add_argument('--runtime', type=Path, default=V2 / 'runtime' / 'llama-fix')
    p.add_argument('--clef', default='clef-flash-iq4xs-b', help='models.lock.json key of the Clef-Flash file')
    p.add_argument('--device', help='llama.cpp device, e.g. Vulkan0')
    p.add_argument('--eval', default='spec,plumb,clef', help='roles whose suites are answered (all three stay loaded)')
    p.add_argument('--banking', type=int, default=1000)
    p.add_argument('--limit', type=int)
    p.add_argument('--stress', type=int, default=60, help='test2 records sent to the three servers at the same time')
    p.add_argument('--results', type=Path, default=HERE / 'results')
    args = p.parse_args()
    exe = args.runtime / ('llama-server.exe' if sys.platform == 'win32' else 'llama-server')
    models = V2 / 'models'
    clef_file = models / LOCK['models'][args.clef]['file']
    roles = [   # largest first
        {'role': 'clef', 'name': f'trio-{args.clef}', 'model': clef_file, 'kind': 'systemone', 'ctx': 8192, 'batch': 4096, 'parallel': 1,
         'extra': OUTPUT_ON_CPU, 'suites': 'jevbench,test2,banking77', 'port': 18941},
        {'role': 'spec', 'name': 'trio-spec-q8', 'model': models / LOCK['built']['spec-q8']['file'], 'kind': 'systemone', 'ctx': 12288,
         'batch': 3072, 'parallel': 2, 'extra': OUTPUT_ON_CPU, 'suites': 'jevbench,test2,banking77', 'port': 18942},
        {'role': 'plumb', 'name': 'trio-plumb-q4', 'model': models / LOCK['models']['plumb-q4']['file'], 'kind': 'direct', 'ctx': 8192,
         'batch': 512, 'parallel': 1, 'extra': OUTPUT_ON_CPU + ['-fa', 'on', '-ctk', 'q8_0', '-ctv', 'q8_0', '--cache-ram', '1024'],
         'suites': 'jevbench,test2',
         'port': 18943},
    ]
    for r in roles:
        if not r['model'].is_file():
            raise SystemExit(f'missing model: {r["model"]}')
    before = free_mib(exe, args.device)
    print(f'free before: {before}', flush=True)
    summary = {'runtime': str(args.runtime), 'clef': args.clef, 'free_before': before, 'servers': {}}
    servers = {}
    try:
        for r in roles:
            out = args.results / r['name']
            out.mkdir(parents=True, exist_ok=True)
            server = Server(exe, r['model'], r['port'], out / 'server.log', args.device, r['ctx'], r['batch'], r['parallel'], r['extra'])
            load = server.wait()
            servers[r['role']] = server
            info = {'model': r['model'].name, 'bytes': r['model'].stat().st_size, 'kind': r['kind'], 'load_seconds': round(load, 2),
                    'args': server.args[1:], 'memory': memory_from_log(server.log)}
            (out / 'target.json').write_text(json.dumps(info, indent=2), encoding='utf-8')
            summary['servers'][r['role']] = info
            print(f"{r['role']}: loaded in {load:.1f} s, device {info['memory']['total_device_mib']} MiB", flush=True)
        summary['free_all_loaded'] = free_mib(exe, args.device)
        print(f"free with all three loaded: {summary['free_all_loaded']}", flush=True)

        readers = {r['role']: (DirectReadout(args.v1, args.v1 / 'models' / 'qwen35-4b', servers[r['role']].url, warm_prefix=True)
                               if r['kind'] == 'direct' else None) for r in roles}
        for r in roles:
            if r['role'] in args.eval.split(','):
                answer_suites(servers[r['role']], readers[r['role']], args.v1, args.results / r['name'], r['suites'].split(','), args.banking, args.limit)

        # stress: the same test2 records to the three servers at once
        items = suites.load(args.v1, 'test2')[:args.stress]
        errors, latencies = {r['role']: 0 for r in roles}, {r['role']: [] for r in roles}

        def worker(role):
            for item in items:
                t = time.perf_counter()
                try:
                    if readers[role]:
                        readers[role].evaluate(item)
                    else:
                        post(servers[role].url + '/v1/systemone', suites.payload(item))
                    latencies[role].append(time.perf_counter() - t)
                except Exception:
                    errors[role] += 1
        began = time.perf_counter()
        threads = [threading.Thread(target=worker, args=(r['role'],)) for r in roles]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        summary['stress'] = {'records': len(items), 'wall_seconds': round(time.perf_counter() - began, 1), 'errors': errors,
                             'median_latency_s': {k: round(sorted(v)[len(v) // 2], 3) if v else None for k, v in latencies.items()},
                             'all_alive': all(s.proc.poll() is None for s in servers.values())}
        summary['free_after_stress'] = free_mib(exe, args.device)
        for r in roles:
            summary['servers'][r['role']]['memory_after'] = memory_from_log(servers[r['role']].log)
        print(json.dumps(summary['stress'], indent=2), flush=True)
    except Exception as exc:
        summary['error'] = f'{type(exc).__name__}: {exc}'
        print(f'trio failed: {summary["error"]}', flush=True)
        raise
    finally:
        for s in servers.values():
            s.stop()
        path = args.results / f'trio-{args.clef}.json'
        path.write_text(json.dumps(summary, indent=2, default=str), encoding='utf-8')
        print(f'trio summary: {path}', flush=True)


if __name__ == '__main__':
    main()
