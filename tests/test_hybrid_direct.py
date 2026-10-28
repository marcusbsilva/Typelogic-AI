"""Generalist readout helpers and specialist/generalist routing, without loading any model."""
import json
import math

import pytest

from typelogic.core import validate, answers
from typelogic.direct import options_of, softmax, structured_state, messages, LETTERS
from typelogic.hybrid import HybridEngine, GENERALIST_MAX_OPTIONS, product_of_experts


def test_option_mapping_follows_semif_conversion():
    _, qs, _ = validate({'state': 's', 'questions': {
        'n': {'type': 'noul', 'instructions': 'Allowed?', 'criteria': {'true': 'All conditions hold', 'false': 'Something is missing'}},
        'm': {'type': 'noul', 'instructions': 'Allowed?'},
        'c': {'type': 'choice', 'instructions': 'Route', 'criteria': {'billing': 'Payments', 'it': 'Software'}},
        's': {'type': 'score', 'instructions': 'Rate', 'criteria': ['low', 'high']}}})
    by = {q['id']: q for q in qs}
    assert options_of(by['n']) == (['true', 'false'], ['true: All conditions hold', 'false: Something is missing'])
    assert options_of(by['m']) == (['true', 'false'], ['true: The proposition is true.', 'false: The proposition is false.'])
    ids, texts = options_of(by['c'])
    assert set(ids) == {'billing', 'it'} and all(t.startswith(i + ': ') for i, t in zip(ids, texts))
    assert options_of(by['s']) == (['0', '1'], ['0: low', '1: high'])
    payload = json.loads(messages({'a': 1}, 'Rate', ['0: low', '1: high'])[1]['content'])
    assert payload == {'evidence': {'a': 1}, 'criterion': 'Rate', 'options': [{'letter': 'A', 'description': '0: low'}, {'letter': 'B', 'description': '1: high'}]}


def test_softmax_temperature_and_structured_state():
    p = softmax([2.0, 0.0])
    q = softmax([2.0, 0.0], 2.0)
    assert math.isclose(sum(p), 1) and p[0] > q[0] > .5
    with pytest.raises(ValueError):
        softmax([1.0])
    assert structured_state('{"x": [1, 2]}') == {'x': [1, 2]} and structured_state('{not json') == '{not json'
    assert len(LETTERS) == GENERALIST_MAX_OPTIONS


class Fake:
    def __init__(self, name, value):
        self.name, self.value, self.calls, self.status, self.error, self.cache = name, value, [], 'ready', None, {}
        self.root, self.backend = name, name

    def load(self):
        self.status = 'ready'

    def evaluate(self, payload):
        state, qs, threshold = validate(payload)
        self.calls.append(sorted(payload['questions']))
        rows = [[self.value if i == 0 else (1 - self.value) / (len(q['keys']) - 1) for i in range(len(q['keys']))] for q in qs]
        return {'model': self.name, 'checkpoint_sha256': 'abc' if self.name == 'specialist' else None, 'backend': self.name,
                'answers': answers(qs, rows, threshold, 'test'), 'usage': {'input_tokens': 10}, 'latency_ms': 1}

    def health(self):
        return {'status': self.status, 'error': None, 'model': self.name}


def request(n_options):
    return {'state': 'A customer asks something.', 'questions': {
        'small': {'type': 'choice', 'instructions': 'Pick', 'criteria': {f'k{i}': f'option {i}' for i in range(3)}},
        'flag': {'type': 'noul', 'instructions': 'Refund?'},
        'catalog': {'type': 'choice', 'instructions': 'Intent', 'criteria': {f'i{i}': f'intent {i}' for i in range(n_options)}}}}


def test_routing_by_option_count_keeps_order_and_marks_engines():
    s, g = Fake('specialist', .7), Fake('generalist', .9)
    engine = HybridEngine(s, g)
    out = engine.evaluate(request(GENERALIST_MAX_OPTIONS + 1))
    assert list(out['answers']) == ['small', 'flag', 'catalog']
    assert out['routing']['questions'] == {'small': 'generalist', 'flag': 'generalist', 'catalog': 'specialist'}
    assert s.calls == [['catalog']] and g.calls == [['flag', 'small']]
    assert out['answers']['catalog']['engine'] == 'specialist' and out['answers']['flag']['engine'] == 'generalist'
    assert out['checkpoint_sha256'] == 'abc' and out['model'] == 'typelogic-hybrid' and out['usage']['input_tokens'] == 20
    out = engine.evaluate(request(GENERALIST_MAX_OPTIONS))
    assert set(out['routing']['questions'].values()) == {'generalist'} and len(s.calls) == 1


def test_forced_specialist_and_missing_generalist():
    s, g = Fake('specialist', .7), Fake('generalist', .9)
    out = HybridEngine(s, g, force='specialist').evaluate(request(5))
    assert set(out['routing']['questions'].values()) == {'specialist'} and not g.calls
    out = HybridEngine(Fake('specialist', .6), None).evaluate(request(5))
    assert set(out['routing']['questions'].values()) == {'specialist'}
    with pytest.raises(ValueError):
        HybridEngine(s, g, force='gpu')


def test_status_follows_both_engines_and_server_overrides():
    s, g = Fake('specialist', .7), Fake('generalist', .9)
    engine = HybridEngine(s, g)
    assert engine.status == 'ready'
    engine.status = 'loading'
    assert engine.status == 'loading'
    engine.load()
    assert engine.status == 'ready' and engine.health()['generalist']['model'] == 'generalist'
    g.status = 'error'
    assert engine.status == 'error'


class ScriptedDirect:
    """DirectEngine with the model replaced by a scoring function of (option id, letter position)."""

    def __init__(self, tmp_path, score, orders=None):
        from typelogic.direct import DirectEngine
        self.engine = DirectEngine(tmp_path, device='cpu', orders=orders)
        self.engine.model = object()
        self.engine.status = 'ready'
        self.score = score
        engine = self.engine
        self.current = None

        def encode(state, q, order=None):
            option_ids = options_of(q, order)[0]
            self.current = option_ids
            return list(range(len(option_ids))), option_ids, 'h'
        engine.encode = encode
        engine.logits = lambda ids, count: [self.score(option, position) for position, option in enumerate(self.current)]


@pytest.fixture(autouse=True)
def no_engine_environment(monkeypatch):
    for name in ('TYPELOGIC_OLLAMA_MODEL', 'TYPELOGIC_OLLAMA_URL', 'TYPELOGIC_DEVICE', 'TYPELOGIC_ROUTE', 'TYPELOGIC_LAZY_SPECIALIST', 'TYPELOGIC_THREADS', 'TYPELOGIC_DIRECT_ORDERS', 'TYPELOGIC_HYBRID_MODE', 'TYPELOGIC_ENSEMBLE_WEIGHT'):
        monkeypatch.delenv(name, raising=False)


def test_direct_evaluate_maps_noul_score_and_checks_order(tmp_path):
    stable = ScriptedDirect(tmp_path, lambda option, position: {'true': 2.0, 'false': 0.0, 'b': 3.0, '1': 1.0}.get(option, 0.0)).engine
    out = stable.evaluate({'state': 'x', 'questions': {
        'n': {'type': 'noul', 'instructions': 'ok?'},
        'c': {'type': 'choice', 'instructions': 'pick', 'criteria': {'a': 'A', 'b': 'B', 'c': 'C'}},
        's': {'type': 'score', 'instructions': 'rate', 'criteria': ['lo', 'hi']}}})
    a = out['answers']
    assert a['n']['probabilities']['true'] > .85 and a['c']['choice'] == 'b' and a['s']['probabilities']['1'] > .7
    assert a['c']['order_diagnostic']['checked'] and not a['c']['order_diagnostic']['unstable']
    biased = ScriptedDirect(tmp_path, lambda option, position: 3.0 if position == 0 else 0.0).engine
    a = biased.evaluate({'state': 'x', 'questions': {'c': {'type': 'choice', 'instructions': 'pick', 'criteria': {'a': 'A', 'b': 'B', 'c': 'C'}}}})['answers']
    assert a['c']['order_diagnostic']['unstable'] and 'option_order_sensitive' in a['c']['review_reasons'] and a['c']['recommendation'] == 'review'
    a = biased.evaluate({'state': 'x', 'permutation_check': False, 'questions': {'c': {'type': 'choice', 'instructions': 'pick', 'criteria': {'a': 'A', 'b': 'B'}}}})['answers']
    assert a['c']['order_diagnostic'] == {'checked': False}


def test_calibration_file_is_per_backend(tmp_path):
    from typelogic.direct import DirectEngine, calibration_name
    (tmp_path / calibration_name('torch')).write_text(json.dumps({'temperature': 1.3}))
    assert DirectEngine(tmp_path).temperature == 1.3
    other = DirectEngine(tmp_path, ollama_model='qwen3.5:4b')
    assert other.backend == 'ollama' and other.temperature == 1.0 and other.calibration_file == 'direct-calibration-ollama-qwen3.5_4b.json'


def test_two_order_averaging_cancels_pure_position_bias_and_keeps_ids(tmp_path):
    # a model that only likes the first letter: single order picks 'a'; averaging with the reversed order is uniform
    # for 2 options, and the remapped ids keep each option's own description
    biased = ScriptedDirect(tmp_path, lambda option, position: 3.0 if position == 0 else 0.0, orders=2).engine
    a = biased.evaluate({'state': 'x', 'questions': {'c': {'type': 'choice', 'instructions': 'pick', 'criteria': {'a': 'A', 'b': 'B'}},
                                                    'n': {'type': 'noul', 'instructions': 'ok?'}}})['answers']
    assert abs(a['c']['probabilities']['a'] - .5) < 1e-6 and abs(a['n']['probabilities']['true'] - .5) < 1e-6
    assert a['c']['order_diagnostic']['averaged_orders'] == 2 and a['c']['order_diagnostic']['unstable']
    content = ScriptedDirect(tmp_path, lambda option, position: {'b': 3.0}.get(option, 0.0) + (1.0 if position == 0 else 0.0), orders=2).engine
    a = content.evaluate({'state': 'x', 'questions': {'c': {'type': 'choice', 'instructions': 'pick', 'criteria': {'a': 'A', 'b': 'B', 'c': 'C'}}}})['answers']
    assert a['c']['choice'] == 'b' and not a['c']['order_diagnostic']['top_choice_changed']
    assert options_of({'type': 'choice', 'keys': ['a', 'b'], 'criteria': {'a': 'A', 'b': 'B'}}, [1, 0]) == (['b', 'a'], ['b: B', 'a: A'])
    with pytest.raises(ValueError):
        options_of({'type': 'choice', 'keys': ['a', 'b'], 'criteria': {}}, [0, 0])


def test_two_order_and_per_type_calibration_are_separate_files(tmp_path):
    from typelogic.direct import DirectEngine, calibration_name
    assert calibration_name('ollama', 'crh225/plumb-4b', orders=2) == 'direct-calibration-ollama-crh225_plumb-4b-avg2.json'
    (tmp_path / calibration_name('torch')).write_text(json.dumps({'temperature': 1.3}))
    assert DirectEngine(tmp_path, orders=2).temperature == 1.0
    (tmp_path / calibration_name('torch', orders=2)).write_text(json.dumps({'temperature': 1.5, 'temperature_by_type': {'noul': 2.0}}))
    engine = DirectEngine(tmp_path, orders=2)
    assert engine.temperature_for('noul') == 2.0 and engine.temperature_for('choice') == 1.5 and engine.calibration_file.endswith('-avg2.json')
    with pytest.raises(ValueError):
        DirectEngine(tmp_path, orders=3)


def test_specialist_is_loaded_lazily_on_the_first_catalog_question():
    s, g = Fake('specialist', .7), Fake('generalist', .9)
    s.status = g.status = 'not_loaded'
    loads = []
    s.load = lambda: (loads.append('specialist'), setattr(s, 'status', 'ready'))
    engine = HybridEngine(s, g, lazy_specialist=True)
    engine.load()
    assert loads == [] and engine.status == 'ready'
    engine.evaluate(request(5))
    assert loads == [] and s.calls == []
    engine.evaluate(request(GENERALIST_MAX_OPTIONS + 1))
    assert loads == ['specialist'] and s.calls == [['catalog']]
    eager = HybridEngine(Fake('specialist', .7), g, lazy_specialist=False)
    eager.specialist.status = 'not_loaded'
    eager.load()
    assert eager.specialist.status == 'ready'


def test_ensemble_mode_combines_both_engines_below_17_options():
    s, g = Fake('specialist', .7), Fake('generalist', .9)
    engine = HybridEngine(s, g, mode='ensemble', weight=.55)
    out = engine.evaluate(request(GENERALIST_MAX_OPTIONS + 1))
    assert out['routing']['questions'] == {'small': 'ensemble', 'flag': 'ensemble', 'catalog': 'specialist'}
    assert sorted(map(tuple, s.calls)) == [('catalog',), ('flag', 'small')] and g.calls == [['flag', 'small']]
    small = out['answers']['small']
    expected = product_of_experts(small['components']['generalist'], small['components']['specialist'], .55)
    assert small['engine'] == 'ensemble' and all(math.isclose(small['probabilities'][k], v) for k, v in expected.items())
    assert .7 < small['probabilities']['k0'] < .9 + 1e-9 and small['requires_review'] and not small['eligible_for_automation']
    assert small['calibration'] == 'not_validated_for_your_domain' and small['choice'] == 'k0'   # Fake components are not calibrated
    assert out['answers']['catalog']['engine'] == 'specialist' and out['routing']['mode'] == 'ensemble'
    p = product_of_experts({'a': .9, 'b': .1}, {'a': .2, 'b': .8}, 1.0)
    assert math.isclose(p['a'], .9) and math.isclose(sum(product_of_experts({'a': .5, 'b': .5}, {'a': .2, 'b': .8}, .5).values()), 1)
    with pytest.raises(ValueError):
        HybridEngine(s, g, mode='vote')
    with pytest.raises(ValueError):
        HybridEngine(s, g, mode='ensemble', weight=1.5)


def test_ensemble_falls_back_to_generalist_when_specialist_cannot_read():
    s, g = Fake('specialist', .7), Fake('generalist', .9)

    def overflow(payload):
        raise ValueError('state exceeds 2048 tokens')
    s.evaluate = overflow
    out = HybridEngine(s, g, mode='ensemble').evaluate(request(5))
    assert {a['engine'] for a in out['answers'].values()} == {'generalist'} and 'ensemble_note' in out
    assert math.isclose(out['answers']['small']['probabilities']['k0'], .9)


def test_cpu_fallback_needs_weights_not_just_the_tokenizer(tmp_path):
    from typelogic.direct import DirectEngine
    (tmp_path / 'config.json').write_text('{}')
    assert not DirectEngine(tmp_path).has_local_weights()
    (tmp_path / 'model.safetensors').write_bytes(b'')
    assert DirectEngine(tmp_path).has_local_weights()


def test_failed_lazy_specialist_does_not_block_the_generalist_and_is_retried():
    s, g = Fake('specialist', .7), Fake('generalist', .9)
    s.status = 'not_loaded'
    attempts = []

    def broken_load():
        attempts.append(1)
        s.status, s.error = 'error', 'weights missing'
        raise FileNotFoundError('weights missing')
    s.load = broken_load
    engine = HybridEngine(s, g, lazy_specialist=True)
    with pytest.raises(RuntimeError, match='Specialist unavailable'):
        engine.evaluate(request(GENERALIST_MAX_OPTIONS + 1))
    assert engine.status == 'ready' and engine.error is None
    out = engine.evaluate(request(5))                       # general questions still answered
    assert set(out['routing']['questions'].values()) == {'generalist'}
    engine.load()                                           # retry button: the specialist is tried again on demand
    assert s.status == 'not_loaded'
    with pytest.raises(RuntimeError):
        engine.evaluate(request(GENERALIST_MAX_OPTIONS + 1))
    assert len(attempts) == 2
    ens = HybridEngine(s, g, lazy_specialist=True, mode='ensemble').evaluate(request(5))
    assert {a['engine'] for a in ens['answers'].values()} == {'generalist'} and 'ensemble_note' in ens


def test_ensemble_label_is_not_validated_when_a_component_is_not():
    s, g = Fake('specialist', .7), Fake('generalist', .9)
    out = HybridEngine(s, g, mode='ensemble').evaluate(request(5))
    assert out['answers']['small']['calibration'] == 'not_validated_for_your_domain'   # Fake answers carry 'test'


def test_direct_calibration_is_ignored_when_weights_or_orders_differ(tmp_path):
    from typelogic.direct import DirectEngine, calibration_name
    (tmp_path / 'model.safetensors').write_bytes(b'1234')
    (tmp_path / 'config.json').write_text('{}')
    import hashlib
    files = {'model.safetensors': {'bytes': 4}, 'config.json': {'sha256': hashlib.sha256(b'{}').hexdigest()}}
    (tmp_path / calibration_name('torch')).write_text(json.dumps({'temperature': 1.3, 'model_files': files, 'orders': 1}))
    assert DirectEngine(tmp_path).temperature == 1.3
    (tmp_path / 'model.safetensors').write_bytes(b'12345')
    engine = DirectEngine(tmp_path)
    assert engine.temperature == 1.0 and engine.calibration == 'not_validated_for_your_domain' and 'differs' in engine.calibration_mismatch
    (tmp_path / calibration_name('torch', orders=2)).write_text(json.dumps({'temperature': 1.1, 'orders': 1}))
    assert DirectEngine(tmp_path, orders=2).calibration_mismatch.startswith('fitted for orders=1')
