import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from typelogic.jev_distill import decide, blend_training_record, preflight_rows
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from build_jev_targets import build
from collect_jev_intents import digest, select
from typelogic.learning import file_sha256


class JevDistillTests(unittest.TestCase):
    def setUp(self):
        self.row = {
            'id': 'banking77:train:1', 'split': 'train', 'state': 'My card has not arrived.',
            'group_id': 'one', 'source_id': 'banking77', 'gold_label': 'arrival',
            'questions': {'intent': {'type': 'choice', 'instructions': 'Choose intent.',
                                     'criteria': {'arrival': 'Card arrival', 'pin': 'PIN change'}}},
            'targets': {'intent': {'arrival': 1., 'pin': 0.}},
        }

    def test_decision_request_and_exact_option_ids(self):
        seen = []
        def transport(url, body, key):
            seen.append((url, body, key))
            return {'id': 'g1', 'model': 'typesafe/jev-1.13-20260917',
                    'answers': {'intent': {'type': 'choice', 'choice': 'arrival',
                                           'probabilities': {'arrival': .8, 'pin': .2}}},
                    'usage': {'cost': .001}}
        result = decide(self.row, 'secret', transport=transport)
        self.assertEqual(seen[0][0], 'https://openrouter.ai/api/alpha/decisions')
        self.assertEqual(seen[0][1]['questions'], self.row['questions'])
        self.assertEqual(result['probabilities'], {'arrival': .8, 'pin': .2})
        self.assertNotIn('secret', json.dumps(result))

    def test_rejects_missing_alternative(self):
        with self.assertRaises(ValueError):
            decide(self.row, 'secret', transport=lambda *a: {'answers': {
                'intent': {'type': 'choice', 'probabilities': {'arrival': 1.}}}})

    def test_blend_agreement_and_keep_gold_on_disagreement(self):
        ok = blend_training_record(self.row, {'arrival': .8, 'pin': .2}, .3)
        self.assertAlmostEqual(ok['targets']['intent']['arrival'], .94)
        self.assertEqual(ok['gold_label'], self.row['gold_label'])
        bad = blend_training_record(self.row, {'arrival': .1, 'pin': .9}, .3)
        self.assertEqual(bad['targets'], self.row['targets'])
        self.assertTrue(bad['distillation']['needs_review'])

    def test_preflight_does_not_retain_encoded_records(self):
        rows = [{'id': 'a'}, {'id': 'b'}]
        result = preflight_rows({'train': rows}, lambda row: {'ids': [1] * (2 if row['id']=='a' else 4)})
        self.assertEqual(result['train'], {'rows': 2, 'max_tokens': 4, 'longest_id': 'b'})

    def test_collect_only_train_and_build_separate_distilled_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); train = root/'train.jsonl'; teachers = root/'teachers'
            teachers.mkdir();out = root/'distilled.jsonl'
            train.write_text(json.dumps(self.row)+'\n', encoding='utf-8')
            self.assertEqual(len(select(train, 1, 5)), 1)
            forbidden = dict(self.row, split='test')
            train.write_text(json.dumps(forbidden)+'\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                select(train, 1, 5)
            train.write_text(json.dumps(self.row)+'\n', encoding='utf-8')
            key = digest(self.row)
            (teachers/'identity.json').write_text(json.dumps({'model': 'typesafe/jev-1.13',
                  'train_sha256': file_sha256(train), 'ids': [self.row['id']],
                  'row_sha256': [key]}), encoding='utf-8')
            (teachers/(key+'.json')).write_text(json.dumps({'id':self.row['id'], 'split':'train',
                'source_id':self.row['source_id'], 'row_sha256':key,
                'model':'typesafe/jev-1.13-20260917',
                'probabilities': {'arrival':.8,'pin':.2}}),encoding='utf-8')
            report = build(train, teachers, out, .3)
            self.assertEqual(report['blended_agreeing'], 1)
            self.assertEqual(json.loads(out.read_text())['targets']['intent']['arrival'], .94)
            with self.assertRaises(ValueError):
                build(train, teachers, out, .3)


if __name__ == '__main__':
    unittest.main()
