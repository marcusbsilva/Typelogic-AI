import sys
import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from typelogic.jev_distill import normalize_wire_probabilities, decide, TeacherPayloadError
import collect_jev_intents as collector


class WireTests(unittest.TestCase):
    def test_cent_rounding_feasible_sums(self):
        for raw in ({'a':.33,'b':.33,'c':.33}, {'a':.34,'b':.34,'c':.33}):
            normalized,meta=normalize_wire_probabilities(raw,list(raw))
            self.assertAlmostEqual(sum(normalized.values()),1.)
            self.assertEqual(meta['method'],'cent_rounding_normalization')
            self.assertEqual(meta['wire_probabilities'],raw)

    def test_nonrounded_malformed_or_large_corrections_rejected(self):
        for raw in ({'a':.4,'b':.4}, {'a':.4001,'b':.59},
                    {'a':1.01,'b':0.}, {'a':float('nan'),'b':1.},
                    {'a':True,'b':0.}, {'a':0.,'b':0.}):
            with self.assertRaises(ValueError):
                normalize_wire_probabilities(raw,list(raw))

    def test_reject_missing_id(self):
        with self.assertRaises(ValueError):
            normalize_wire_probabilities({'a':1.},['a','b'])

    def test_exact_distribution_and_many_candidates(self):
        raw={str(i):0. for i in range(77)};raw['0']=.42;raw['1']=.32;raw['2']=.25
        p,m=normalize_wire_probabilities(raw,list(raw))
        self.assertAlmostEqual(sum(p.values()),1.)
        self.assertAlmostEqual(m['wire_sum'],.99)
        raw={'a':.123456789,'b':.876543211}
        _,meta=normalize_wire_probabilities(raw,list(raw))
        self.assertEqual(meta['method'],'sum_normalization')

    def test_invalid_response_is_recoverable_without_another_request(self):
        row={'id':'one','source_id':'banking77','split':'train','state':'State.',
             'questions':{'intent':{'type':'choice','instructions':'Choose.',
                 'criteria':{'a':'A','b':'B'}}},'targets':{'intent':{'a':1.,'b':0.}}}
        raw={'model':'typesafe/jev-1.13','id':'generation','usage':{'cost':.001},
             'answers':{'intent':{'type':'choice','probabilities':{'a':.4,'b':.4}}}}
        with self.assertRaises(TeacherPayloadError) as raised:
            decide(row,'secret',transport=lambda *a:raw)
        self.assertEqual(raised.exception.response['id'],'generation')
        self.assertNotIn('secret',str(raised.exception.response))

    def test_collector_preserves_rejection_and_resume_does_not_repeat_paid_call(self):
        row={'id':'one','source_id':'banking77','split':'train','state':'State.','gold_label':'a',
             'questions':{'intent':{'type':'choice','instructions':'Choose.',
                 'criteria':{'a':'A','b':'B'}}},'targets':{'intent':{'a':1.,'b':0.}}}
        calls=[]
        def transport(*args):
            calls.append(1)
            return {'model':'typesafe/jev-1.13','id':'generation','usage':{'cost':.001},
                'answers':{'intent':{'type':'choice','probabilities':{'a':.4,'b':.4}}}}
        def fake_decide(row,key,**kwargs):
            return decide(row,key,transport=kwargs.get('transport',transport))
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);train=root/'train.jsonl';train.write_text(json.dumps(row)+'\n')
            argv=['collector','--train-file',str(train),'--out',str(root/'out'),'--execute']
            with patch.dict('os.environ',{'OPENROUTER_API_KEY':'secret'}),patch.object(collector,'decide',fake_decide),redirect_stdout(io.StringIO()):
                with patch.object(sys,'argv',argv),self.assertRaises(ValueError):collector.main()
                self.assertEqual(len(list((root/'out/rejected').glob('*.json'))),1)
                with patch.object(sys,'argv',argv+['--resume']),self.assertRaises(ValueError):collector.main()
            self.assertEqual(len(calls),1)


if __name__=='__main__':unittest.main()
