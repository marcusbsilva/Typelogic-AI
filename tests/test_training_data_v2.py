import random,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'typelogic'))
from training_data_v2 import permute_record,check_groups
class Tests(unittest.TestCase):
 def test_target_alignment(self):
  q={'type':'choice','options':['A','B','C'],'target':[.1,.7,.2],'label':1}
  r={'questions':[q]};s=permute_record(r,random.Random(4))['questions'][0]
  self.assertEqual(dict(zip(s['options'],s['target'])),dict(zip(q['options'],q['target'])))
  self.assertEqual(s['options'][s['label']],'B');self.assertEqual(q['options'],['A','B','C'])
 def test_score_order_preserved(self):
  q={'type':'score','options':['low','high'],'target':[1,0],'label':0}
  self.assertEqual(permute_record({'questions':[q]},random.Random(4))['questions'][0],q)
 def test_repeatable(self):
  r={'questions':[{'type':'choice','options':['A','B','C'],'target':[0,1,0],'label':0}]}
  self.assertEqual(permute_record(r,random.Random(8)),permute_record(r,random.Random(8)))
 def test_translation_leak(self):
  with self.assertRaises(ValueError):check_groups({'train':[{'group_id':'same'}],'test':[{'group_id':'same'}]})
 def test_missing_group(self):
  with self.assertRaises(ValueError):check_groups({'train':[{}]})
 def test_group_within_split_allowed(self):
  check_groups({'train':[{'group_id':'a'},{'group_id':'a'}],'test':[{'group_id':'b'}]})
if __name__=='__main__':unittest.main()
