"""Generalist decisions by direct option-letter logits (zero-shot, no trained head).

Port of the direct readout of SemIf (formerly OpenJev, https://github.com/TheoLeeCJ/SemIf, MIT License,
Copyright (c) 2026 TheoLeeCJ): an instruction-tuned causal LM sees the state, the criterion and lettered
options in one JSON message, and one forward pass gives the next-token logits; the softmax is taken over the
declared answer letters only. No token is generated. Option mapping follows SemIf's conversion of typed
records: noul -> options true/false (criterion text or "The proposition is <id>."), choice -> "<key>: <text>",
score -> "<i>: <level>" (score is an extension; SemIf excludes it).

Backends: PyTorch on cpu/xpu (local Hugging Face folder), llama.cpp (local GGUF through llama-cpp-python), or a
local Ollama server on the loopback address (GGUF models already pulled for other local apps, e.g. on the GPU through
Vulkan). Ollama exposes at most 20 top log-probabilities per position: letters outside that list get the floor
min(top)-ln(1000) and the answer is flagged; the prompt is sent in raw mode, rendered by the reference tokenizer.
Probabilities are conditional on the supplied options; calibrate before trusting them as confidence.

Optional two-order averaging (TYPELOGIC_DIRECT_ORDERS=2): every question is also read with its options in reverse
order and the two distributions are averaged option by option, which reduces position bias (the "reflex" readout
uses the same idea). It doubles the forward passes for noul/score questions; for choice questions with the order
check on, the reversed pass is reused. A temperature is fitted for this configuration separately (file suffix -avg2),
optionally per question type ("temperature_by_type" in the calibration file).
"""
import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path

from .core import answers, validate
from .offline import install

LETTERS = 'ABCDEFGHIJKLMNOP'
SYSTEM = ('Apply the supplied criterion to the supplied evidence. Choose exactly one listed option. '
          'Respond with only its uppercase letter, with no explanation or reasoning.')
PROMPT_VERSION = 'semif-direct-options-v1'


def options_of(question, order=None):
    """(semantic ids in prompt order, option descriptions) for a validated TypeLogic question.

    order: optional permutation (list of indices into the canonical option order) used for order checks/averaging."""
    criteria = question.get('criteria') or {}
    if question['type'] == 'noul':
        ids = ['true', 'false']
        texts = [criteria.get(k) or f'The proposition is {k}.' for k in ids]
    elif question['type'] == 'choice':
        ids = list(question['keys'])
        texts = [criteria.get(k) or k for k in ids]
    else:
        ids = list(question['keys'])
        texts = list(question['criteria'])
    if not 2 <= len(ids) <= len(LETTERS):
        raise ValueError('Direct readout supports 2-16 options')
    if order is not None:
        if sorted(order) != list(range(len(ids))):
            raise ValueError('order must be a permutation of the option indices')
        ids, texts = [ids[i] for i in order], [texts[i] for i in order]
    return ids, [f'{i}: {t}' for i, t in zip(ids, texts)]


def messages(state, instructions, descriptions):
    payload = {'evidence': state, 'criterion': instructions,
               'options': [{'letter': LETTERS[i], 'description': d} for i, d in enumerate(descriptions)]}
    return [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]


def softmax(values, temperature=1.0):
    if len(values) < 2 or any(not math.isfinite(v) for v in values):
        raise ValueError('Need at least two finite option logits')
    scaled = [v / temperature for v in values]
    top = max(scaled)
    weights = [math.exp(v - top) for v in scaled]
    total = sum(weights)
    return [w / total for w in weights]


def structured_state(state):
    """JSON objects/arrays given as text are passed as structured evidence, as SemIf does with structured state."""
    text = state.strip()
    if text[:1] in '{[':
        try:
            return json.loads(text)
        except ValueError:
            pass
    return state


def calibration_name(backend, ollama_model=None, gguf=None, orders=1):
    """direct-calibration-<backend>[-<model tag or GGUF name>][-avg2].json inside the tokenizer/model folder."""
    tag = ollama_model if backend == 'ollama' else Path(gguf).stem if backend == 'llamacpp' and gguf else ''
    tag = ''.join(c if c.isalnum() or c in '.-_' else '_' for c in tag)
    return f'direct-calibration-{backend}' + (f'-{tag}' if tag else '') + ('-avg2' if orders == 2 else '') + '.json'


def view_orders(count, orders):
    """Canonical order, plus the reversed order when two-order averaging is on."""
    return [list(range(count))] + ([list(reversed(range(count)))] if orders == 2 else [])


def average_views(views):
    """Option-wise mean of distributions keyed by option id."""
    return {k: sum(v[k] for v in views) / len(views) for k in views[0]}


class DirectEngine:
    """Same evaluate() contract as DecisionEngine, backed by a frozen instruction model."""

    def __init__(self, model_dir, device=None, dtype=None, backend=None, max_tokens=4096, threads=None, gguf=None, tokenizer_dir=None,
                 ollama_model=None, ollama_url=None, orders=None):
        self.root = Path(model_dir).resolve()
        self.gguf = Path(gguf).resolve() if gguf else None
        self.ollama_model = ollama_model or os.environ.get('TYPELOGIC_OLLAMA_MODEL')
        self.ollama_url = (ollama_url or os.environ.get('TYPELOGIC_OLLAMA_URL') or 'http://127.0.0.1:11434').rstrip('/')
        self.backend = backend or ('llamacpp' if self.gguf else 'ollama' if self.ollama_model else 'torch')
        if self.backend not in ('torch', 'llamacpp', 'ollama'):
            raise ValueError('backend must be torch, llamacpp or ollama')
        self.floored = 0
        self.fallback_reason = None
        self.device = (device or os.environ.get('TYPELOGIC_DEVICE', '') or 'cpu').lower()
        self.dtype = dtype or ('float32' if self.device == 'cpu' else 'bfloat16')
        self.max_tokens = max_tokens
        self.threads = threads or int(os.environ.get('TYPELOGIC_THREADS', '0')) or min(6, os.cpu_count() or 1)
        self.tokenizer_dir = Path(tokenizer_dir).resolve() if tokenizer_dir else self.root
        self.orders = int(orders or os.environ.get('TYPELOGIC_DIRECT_ORDERS', '1') or 1)
        if self.orders not in (1, 2):
            raise ValueError('orders must be 1 or 2')
        self.lock = threading.RLock()
        self.model = self.tokenizer = None
        self.status, self.error = 'not_loaded', None
        # a temperature fitted on one backend/model (e.g. BF16 PyTorch) is never reused for another (e.g. a Q4 GGUF),
        # nor across single-order and two-order readouts
        self._read_calibration(self.backend)

    def _read_calibration(self, backend):
        self.temperature, self.temperature_by_type, self.calibration = 1.0, {}, 'not_validated_for_your_domain'
        calibration = self.root / calibration_name(backend, self.ollama_model, self.gguf, self.orders)
        self.calibration_mismatch = None
        if calibration.is_file():
            value = json.loads(calibration.read_text(encoding='utf-8'))
            self.calibration_mismatch = self._calibration_mismatch(value, backend)
            if self.calibration_mismatch is None:
                self.temperature, self.calibration = float(value['temperature']), 'local_validation_not_general_guarantee'
                self.temperature_by_type = {k: float(v) for k, v in (value.get('temperature_by_type') or {}).items()}
        self.calibration_file = calibration.name

    def _calibration_mismatch(self, value, backend):
        """Why a calibration file does not apply to the loaded configuration (None when it does).

        The file is bound to the readout (orders) and, for PyTorch, to the weight files it was fitted with (sizes of the
        safetensors shards and hashes of the JSON files that were present). Ollama/GGUF weights live outside this folder
        and are identified by the file name (model tag) only."""
        if 'orders' in value and int(value['orders']) != self.orders:   # older files carry the readout in their name only
            return f"fitted for orders={value['orders']}, loaded with orders={self.orders}"
        if backend != 'torch':
            return None
        for name, recorded in (value.get('model_files') or {}).items():
            path = self.root / name
            if 'bytes' in recorded and (not path.is_file() or path.stat().st_size != recorded['bytes']):
                return f'weight file {name} differs from the one used for the fit'
            if 'sha256' in recorded and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != recorded['sha256']:
                return f'{name} differs from the one used for the fit'
        return None

    def temperature_for(self, question_type):
        return self.temperature_by_type.get(question_type, self.temperature)

    # loading -----------------------------------------------------------------------------------------------
    def load(self):
        with self.lock:
            if self.model is not None:
                return
            install()
            self.status = 'loading'
            try:
                import transformers
                self.tokenizer = transformers.AutoTokenizer.from_pretrained(str(self.tokenizer_dir), local_files_only=True)
                if self.backend == 'torch':
                    self._load_torch(transformers)
                elif self.backend == 'llamacpp':
                    self._load_llamacpp()
                else:
                    try:
                        self._load_ollama()
                    except (OSError, FileNotFoundError) as exc:
                        # Ollama not running or model not pulled: optional fallback to the local folder on the CPU
                        if os.environ.get('TYPELOGIC_GENERALIST_FALLBACK', 'cpu').lower() != 'cpu' or not self.has_local_weights():
                            raise
                        self.fallback_reason = f'{type(exc).__name__}: {exc}'[:300]
                        self.backend, self.device, self.dtype = 'torch', 'cpu', 'bfloat16'
                        self._read_calibration('torch')
                        self._load_torch(transformers)
                self.slots = [self._slot(letter) for letter in LETTERS]
                self.status = 'ready'
            except Exception as exc:
                self.status, self.error = 'error', str(exc)
                raise

    def has_local_weights(self):
        """True when the folder holds a complete PyTorch checkpoint (config + safetensors), not just the tokenizer."""
        return (self.root / 'config.json').is_file() and any(self.root.glob('*.safetensors'))

    def _load_torch(self, transformers):
        import torch
        torch.set_num_threads(self.threads)
        if self.device == 'xpu' and not (getattr(torch, 'xpu', None) and torch.xpu.is_available()):
            raise RuntimeError('Intel XPU not available in this PyTorch build')
        config = transformers.AutoConfig.from_pretrained(str(self.root), local_files_only=True)
        cls = transformers.AutoModelForCausalLM
        if config.model_type in ('qwen3_5', 'qwen3_5_text'):
            cls = transformers.Qwen3_5ForCausalLM
            config = config.get_text_config()
        model, info = cls.from_pretrained(str(self.root), config=config, dtype=getattr(torch, self.dtype), local_files_only=True,
                                          low_cpu_mem_usage=True, output_loading_info=True)
        if any(info.get(k) for k in ('missing_keys', 'mismatched_keys', 'error_msgs')):
            raise RuntimeError(f'Checkpoint did not load completely: { {k: info.get(k) for k in ("missing_keys", "mismatched_keys")} }')
        self.model = model.to(self.device).eval()

    def _load_llamacpp(self):
        from llama_cpp import Llama
        if not self.gguf or not self.gguf.is_file():
            raise FileNotFoundError('GGUF file not found')
        self.model = Llama(model_path=str(self.gguf), n_ctx=self.max_tokens + 16, n_threads=self.threads, n_batch=512,
                           logits_all=False, n_gpu_layers=int(os.environ.get('TYPELOGIC_GPU_LAYERS', '0')), verbose=False)

    def _load_ollama(self):
        from urllib.parse import urlparse
        from .offline import allowed
        host = urlparse(self.ollama_url).hostname
        if not self.ollama_model or not allowed((host, 0)):
            raise ValueError('Ollama backend needs a model tag and a loopback URL')
        tags = self._ollama('/api/tags', None)
        names = {m.get('name') for m in tags.get('models', [])} | {m.get('model') for m in tags.get('models', [])}
        if self.ollama_model not in names and self.ollama_model + ':latest' not in names:
            raise FileNotFoundError(f'Ollama model {self.ollama_model!r} is not pulled; run: ollama pull {self.ollama_model}')
        self.model = 'ollama'

    def _ollama(self, path, body):
        import urllib.request
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.ollama_url + path, data=data, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read().decode('utf-8'))

    def _slot(self, letter):
        ids = self.tokenizer.encode(letter, add_special_tokens=False)
        if len(ids) != 1 or self.tokenizer.decode(ids) != letter:
            raise ValueError(f'Answer slot {letter!r} is not one round-trip token')
        return ids[0]

    # scoring -----------------------------------------------------------------------------------------------
    def encode(self, state, question, order=None):
        option_ids, descriptions = options_of(question, order)
        prompt = self.tokenizer.apply_chat_template(messages(structured_state(state), question['instr'], descriptions),
                                                    tokenize=False, add_generation_prompt=True, enable_thinking=False)
        ids = self.tokenizer.encode(prompt, add_special_tokens=False)
        if len(ids) > self.max_tokens:
            raise ValueError(f'{len(ids)} input tokens exceed the direct readout limit of {self.max_tokens}; nothing is truncated')
        return ids, option_ids, hashlib.sha256(prompt.encode()).hexdigest()

    def logits(self, ids, count):
        slots = self.slots[:count]
        if self.backend == 'torch':
            import torch
            with torch.inference_mode():
                x = torch.tensor([ids], dtype=torch.long, device=self.device)
                out = self.model(input_ids=x, attention_mask=torch.ones_like(x), use_cache=False, logits_to_keep=1)
                row = out.logits[0, -1].float()
                return row[slots].cpu().tolist()
        if self.backend == 'llamacpp':
            self.model.reset()
            self.model.eval(ids)
            row = self.model.scores[self.model.n_tokens - 1]
            return [float(row[s]) for s in slots]
        prompt = self.tokenizer.decode(ids)
        reply = self._ollama('/api/generate', {'model': self.ollama_model, 'prompt': prompt, 'raw': True, 'stream': False,
                                               'logprobs': True, 'top_logprobs': 20, 'keep_alive': '30m',
                                               'options': {'temperature': 0, 'num_predict': 1, 'num_ctx': self.max_tokens + 16, 'seed': 0}})
        entries = reply.get('logprobs') or []
        if not entries:
            raise RuntimeError('Ollama returned no logprobs (needs Ollama 0.12 or later)')
        top = {}
        for alt in entries[0].get('top_logprobs') or []:
            token = alt.get('token', '').strip()
            if token in LETTERS and token not in top:
                top[token] = float(alt['logprob'])
        if not top:
            raise RuntimeError('None of the answer letters is among the top log-probabilities')
        floor = min(top.values()) - math.log(1000)
        letters = LETTERS[:count]
        if any(l not in top for l in letters):
            self.floored += 1
        return [top.get(l, floor) for l in letters]

    def _view(self, state, q, order):
        """One reading: (probabilities keyed by option id, input tokens, floored?)."""
        ids, option_ids, _ = self.encode(state, q, order)
        before = self.floored
        probabilities = dict(zip(option_ids, softmax(self.logits(ids, len(option_ids)), self.temperature_for(q['type']))))
        return probabilities, len(ids), self.floored != before

    def _predict(self, state, questions):
        rows, tokens, floored, views = [], 0, set(), {}
        for q in questions:
            count = len(options_of(q)[0])
            views[q['id']] = []
            for order in view_orders(count, self.orders):
                probabilities, used, low = self._view(state, q, order)
                tokens += used
                views[q['id']].append(probabilities)
                if low:
                    floored.add(q['id'])
            probabilities = average_views(views[q['id']])
            rows.append([probabilities[k] for k in q['keys']])
        return rows, tokens, floored, views

    def evaluate(self, payload):
        state, questions, threshold = validate(payload)
        began = time.perf_counter()
        with self.lock:
            self.load()
            rows, tokens, floored, views = self._predict(state, questions)
            result = answers(questions, rows, threshold, self.calibration)
            for qid in floored:
                result[qid]['review_reasons'].append('option_logit_floor'); result[qid]['recommendation'] = 'review'
            if payload.get('permutation_check', True):
                # same two-permutation diagnostic as the specialist: reversed and rotated option order
                for q in questions:
                    if q['type'] != 'choice':
                        continue
                    # single readings in reversed and rotated order, compared with the canonical single reading;
                    # with two-order averaging the reversed reading is already available and is reused
                    count = len(options_of(q)[0])
                    original, variants = views[q['id']][0], list(views[q['id']][1:])
                    wanted = [list(range(1, count)) + [0]] if variants else [list(reversed(range(count))), list(range(1, count)) + [0]]
                    for order in wanted:
                        probabilities, extra, _ = self._view(state, q, order)
                        tokens += extra
                        variants.append(probabilities)
                    chosen = result[q['id']]['choice']
                    shift = max(abs(original[key] - v[key]) for v in variants for key in original)
                    flipped = any(max(v, key=v.__getitem__) != chosen for v in [original] + variants)
                    unstable = flipped or shift > .15
                    result[q['id']]['order_diagnostic'] = {'checked': True, 'permutations': 2, 'max_probability_shift': shift, 'top_choice_changed': flipped,
                                                           'unstable': unstable, 'averaged_orders': self.orders,
                                                           'scope': 'two permutations; not a robustness certificate'}
                    if unstable:
                        result[q['id']]['review_reasons'].append('option_order_sensitive'); result[q['id']]['recommendation'] = 'review'
            else:
                for q in questions:
                    if q['type'] == 'choice':
                        result[q['id']]['order_diagnostic'] = {'checked': False}; result[q['id']]['review_reasons'].append('order_check_disabled')
        return {'model': 'typelogic-direct', 'backend': f'direct-{self.backend}', 'base_model': self.root.name,
                'prompt_version': PROMPT_VERSION, 'offline': True, 'answers': result,
                'usage': {'input_tokens': tokens, 'output_tokens': 0, 'questions': len(questions)},
                'latency_ms': round((time.perf_counter() - began) * 1000, 2), 'external_action_executed': False}

    def health(self):
        return {'status': self.status, 'error': self.error, 'offline': True, 'model': 'typelogic-direct', 'backend': self.backend,
                'device': self.device if self.backend == 'torch' else self.backend, 'dtype': self.dtype if self.backend == 'torch' else None,
                'model_dir': str(self.root), 'gguf': str(self.gguf) if self.gguf else None,
                'ollama_model': self.ollama_model if self.backend == 'ollama' else None, 'answers_with_floored_letters': self.floored, 'fallback_reason': self.fallback_reason,
                'temperature': self.temperature, 'temperature_by_type': self.temperature_by_type or None, 'option_orders': self.orders, 'calibration_mismatch': self.calibration_mismatch,
                'calibration': self.calibration, 'calibration_file': self.calibration_file,
                'prompt_version': PROMPT_VERSION}
