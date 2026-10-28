"""Specialist + generalist routing (TYPELOGIC_ENGINE=hybrid).

The trained specialist (Qwen3.5-2B LoRA + pointer head, typelogic/engine.py) reads any number of options in one
pass and is calibrated for its training domain (intent catalogs). The generalist (zero-shot direct option logits,
typelogic/direct.py, e.g. Qwen3.5-4B) is stronger on general rule, policy and multi-step decisions but reads at
most 16 options. Measured on the public JevBench items (research/results/jevbench): specialist 59.3%, generalist
4B 79.7%; on the 8,260-question BANKING77 catalog test the specialist scores 91.8% (questions of 77-151 options).

Routing per question, decided before any model runs:
  * more than 16 options                      -> specialist
  * otherwise                                  -> generalist
  * TYPELOGIC_ROUTE=specialist|generalist forces one engine for every question it can handle.
The answers keep the request order; every answer records the engine that produced it.

Optional ensemble (TYPELOGIC_HYBRID_MODE=ensemble): questions with at most 16 options are read by both engines and
the two distributions are combined as a weighted product of experts, p ~ generalist^w * specialist^(1-w)
(w = TYPELOGIC_ENSEMBLE_WEIGHT, default 0.55, chosen on the calibration and test splits of typelogic-distill-v1).
On the separate test split typelogic-distill-v1-test2 (615 questions) it kept the accuracy of the generalist alone
(87.5% both; paired 95% interval of the difference -1.5 to +1.5 points) and improved the probabilities: NLL
0.452 -> 0.380, Brier 0.141 -> 0.112, ECE 3.7% -> 3.1%. It needs the specialist loaded as well (memory and time).
When the specialist cannot read a question (e.g. state over its token limit) the generalist answers alone.
"""
import math
import os
import threading
import time

from .core import answers as build_answers, validate

GENERALIST_MAX_OPTIONS = 16
ENSEMBLE_WEIGHT = 0.55
_RECOMPUTED_REASONS = {'human_review_required', 'below_threshold', 'abstention_not_declared', 'explicit_abstention'}


def product_of_experts(generalist, specialist, weight):
    """Weighted geometric mean of two distributions over the same keys, renormalised."""
    logs = {k: weight * math.log(max(generalist[k], 1e-12)) + (1 - weight) * math.log(max(specialist[k], 1e-12)) for k in generalist}
    top = max(logs.values())
    total = sum(math.exp(v - top) for v in logs.values())
    return {k: math.exp(v - top) / total for k, v in logs.items()}


class HybridEngine:
    def __init__(self, specialist, generalist=None, force=None, lazy_specialist=None, mode=None, weight=None):
        self.specialist, self.generalist = specialist, generalist
        self.mode = (mode or os.environ.get('TYPELOGIC_HYBRID_MODE', '') or 'route').strip().lower()
        if self.mode not in ('route', 'ensemble'):
            raise ValueError('TYPELOGIC_HYBRID_MODE must be route or ensemble')
        self.weight = float(weight if weight is not None else os.environ.get('TYPELOGIC_ENSEMBLE_WEIGHT', '') or ENSEMBLE_WEIGHT)
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError('TYPELOGIC_ENSEMBLE_WEIGHT must be between 0 and 1')
        # Lazy specialist: loaded on the first question routed to it (saves 4.5-9 GB while only general questions arrive).
        if lazy_specialist is None:
            lazy_specialist = os.environ.get('TYPELOGIC_LAZY_SPECIALIST', '1').strip() != '0'
        self.lazy_specialist = bool(lazy_specialist and generalist is not None)
        self.force = (force or os.environ.get('TYPELOGIC_ROUTE', '')).strip().lower() or None
        if self.force not in (None, 'specialist', 'generalist'):
            raise ValueError('TYPELOGIC_ROUTE must be specialist or generalist')
        self.lock = threading.RLock()
        self._status, self._error = None, None

    # the server sets status/error while it loads in the background
    @property
    def status(self):
        if self._status is not None:
            return self._status
        # a lazy specialist that is not loaded, or failed to load, does not stop the generalist from answering;
        # its error is reported in health()['specialist'] and on the questions that need it
        lazy_idle = self.lazy_specialist and self.specialist.status in ('not_loaded', 'error')
        states = ([] if lazy_idle else [self.specialist.status]) + ([self.generalist.status] if self.generalist else [])
        if 'error' in states:
            return 'error'
        return 'ready' if all(s == 'ready' for s in states) else 'loading' if 'loading' in states else 'not_loaded'

    @status.setter
    def status(self, value):
        self._status = None if value == 'ready' else value

    @property
    def error(self):
        specialist = None if self.lazy_specialist and self.specialist.status != 'loading' else self.specialist.error
        return self._error or specialist or (self.generalist.error if self.generalist else None)

    @error.setter
    def error(self, value):
        self._error = value

    @property
    def cache(self):
        return self.specialist.cache

    def load(self):
        with self.lock:
            self._status = 'loading'
            try:
                if not self.lazy_specialist or self.force == 'specialist':
                    self.specialist.load()
                elif self.specialist.status == 'error':
                    self.specialist.status, self.specialist.error = 'not_loaded', None   # retried on the next catalog question
                if self.generalist:
                    self.generalist.load()
                self._status, self._error = None, None
            except Exception as exc:
                self._status, self._error = 'error', str(exc)
                raise

    def route(self, question):
        if self.generalist is None or len(question['keys']) > GENERALIST_MAX_OPTIONS:
            return 'specialist'
        if self.force == 'specialist':
            return 'specialist'
        if self.mode == 'ensemble' and self.force is None:
            return 'ensemble'
        return 'generalist'

    def evaluate(self, payload):
        state, questions, threshold = validate(payload)
        began = time.perf_counter()
        routes = {q['id']: self.route(q) for q in questions}
        groups = {}
        for qid, engine in routes.items():
            groups.setdefault(engine, []).append(qid)
        replies, ensemble_note = {}, None
        with self.lock:
            if ('specialist' in groups or 'ensemble' in groups) and self.specialist.status != 'ready':
                try:
                    self.specialist.load()   # lazy load; the generalist answers alone until a catalog question arrives
                except Exception as exc:
                    if 'specialist' in groups:
                        raise RuntimeError(f'Specialist unavailable for questions with more than {GENERALIST_MAX_OPTIONS} options: {exc}') from exc
                    ensemble_note = f'specialist unavailable ({type(exc).__name__}: {str(exc)[:160]}); generalist answered alone'
            for engine, ids in groups.items():
                part = dict(payload, questions={qid: payload['questions'][qid] for qid in ids})
                if engine == 'ensemble':
                    replies['generalist_ensemble'] = self.generalist.evaluate(part)
                    if ensemble_note:
                        continue
                    try:
                        replies['specialist_ensemble'] = self.specialist.evaluate(part)
                    except Exception as exc:  # e.g. state longer than the specialist reads: generalist alone
                        ensemble_note = f'specialist unavailable for these questions ({type(exc).__name__}: {str(exc)[:160]}); generalist answered alone'
                    continue
                replies[engine] = (self.specialist if engine == 'specialist' else self.generalist).evaluate(part)
        base = replies.get('specialist') or replies.get('specialist_ensemble') or \
            {k: v for k, v in (replies.get('generalist') or replies['generalist_ensemble']).items() if k != 'answers'}
        response = {k: v for k, v in base.items() if k not in ('answers', 'usage', 'latency_ms', 'backend', 'prefix_cache_hit')}
        answers = {}
        combined = self._combine(questions, routes, replies, threshold) if 'generalist_ensemble' in replies else {}
        for q in questions:
            route = routes[q['id']]
            if route == 'ensemble':
                answer = combined.get(q['id']) or dict(replies['generalist_ensemble']['answers'][q['id']], engine='generalist')
                if q['id'] not in combined and ensemble_note:
                    answer['ensemble_note'] = ensemble_note
            else:
                answer = replies[route]['answers'][q['id']]
                answer['engine'] = route
            answers[q['id']] = answer
        usage = {'input_tokens': 0, 'output_tokens': 0, 'questions': len(questions)}
        for reply in replies.values():
            usage['input_tokens'] += reply.get('usage', {}).get('input_tokens', 0)
        response.update(model='typelogic-hybrid', answers=answers, usage=usage,
                        backend={engine: reply.get('backend') for engine, reply in replies.items()},
                        routing={'rule': f'>{GENERALIST_MAX_OPTIONS} options -> specialist, otherwise ' +
                                 ('both engines (product of experts, generalist weight %.2f)' % self.weight if self.mode == 'ensemble' else 'generalist'),
                                 'mode': self.mode, 'forced': self.force, 'questions': routes},
                        generalist=None if self.generalist is None else {'model_dir': str(self.generalist.root), 'backend': self.generalist.backend},
                        latency_ms=round((time.perf_counter() - began) * 1000, 2))
        if 'specialist' in replies:
            response['prefix_cache_hit'] = replies['specialist'].get('prefix_cache_hit')
        if ensemble_note:
            response['ensemble_note'] = ensemble_note
        response.setdefault('checkpoint_sha256', getattr(self.specialist, 'fingerprint', None))
        return response

    def _combine(self, questions, routes, replies, threshold):
        """Ensemble answers: product of experts of the two distributions, recomputed with the shared answer builder."""
        spec = (replies.get('specialist_ensemble') or {}).get('answers') or {}
        gen = replies['generalist_ensemble']['answers']
        chosen = [q for q in questions if routes[q['id']] == 'ensemble' and q['id'] in spec and q['id'] in gen]
        if not chosen:
            return {}
        mixes = [product_of_experts(gen[q['id']]['probabilities'], spec[q['id']]['probabilities'], self.weight) for q in chosen]
        validated = all(source[q['id']].get('calibration') == 'local_validation_not_general_guarantee' for source in (gen, spec) for q in chosen)
        built = build_answers(chosen, [[m[k] for k in q['keys']] for q, m in zip(chosen, mixes)], threshold,
                              'ensemble_local_validation_not_general_guarantee' if validated else 'not_validated_for_your_domain')
        for q in chosen:
            a = built[q['id']]
            extra = [r for source in (gen[q['id']], spec[q['id']]) for r in source.get('review_reasons', []) if r not in _RECOMPUTED_REASONS]
            for reason in extra:
                if reason not in a['review_reasons']:
                    a['review_reasons'].append(reason)
            if extra:
                a['recommendation'] = 'review'
            if 'order_diagnostic' in gen[q['id']]:
                a['order_diagnostic'] = gen[q['id']]['order_diagnostic']
            a['engine'] = 'ensemble'
            a['components'] = {'generalist': gen[q['id']]['probabilities'], 'specialist': spec[q['id']]['probabilities'],
                               'generalist_weight': self.weight, 'rule': 'p ~ generalist^w * specialist^(1-w)'}
        return built

    def health(self):
        value = dict(self.specialist.health())
        value.update(status=self.status, error=self.error, model='typelogic-hybrid', specialist=self.specialist.health(),
                     generalist=self.generalist.health() if self.generalist else None,
                     routing={'max_generalist_options': GENERALIST_MAX_OPTIONS, 'forced': self.force, 'lazy_specialist': self.lazy_specialist,
                              'mode': self.mode, 'ensemble_weight': self.weight if self.mode == 'ensemble' else None})
        return value


def build_engine(model_dir, use_onnx=True, readout=None):
    """Engine selected by TYPELOGIC_ENGINE (specialist by default; hybrid needs TYPELOGIC_GENERALIST)."""
    from .engine import DecisionEngine
    specialist = DecisionEngine(model_dir, use_onnx, readout=readout)
    mode = os.environ.get('TYPELOGIC_ENGINE', 'specialist').strip().lower()
    if mode not in ('specialist', 'hybrid'):
        raise ValueError('TYPELOGIC_ENGINE must be specialist or hybrid')
    if mode == 'specialist':
        return specialist
    from .direct import DirectEngine
    folder = os.environ.get('TYPELOGIC_GENERALIST', '').strip()
    if not folder:
        raise ValueError('TYPELOGIC_ENGINE=hybrid needs TYPELOGIC_GENERALIST (the generalist model folder)')
    from pathlib import Path
    path = Path(folder)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    generalist = DirectEngine(path, device=os.environ.get('TYPELOGIC_GENERALIST_DEVICE') or None,
                              dtype=os.environ.get('TYPELOGIC_GENERALIST_DTYPE') or None)
    return HybridEngine(specialist, generalist)
