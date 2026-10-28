"""Dataset grouping and target-preserving training augmentation."""
import copy

def permute_record(record,rng):
 result=copy.deepcopy(record)
 for q in result['questions']:
  if q['type']!='choice':continue
  order=list(range(len(q['options'])));rng.shuffle(order)
  q['options']=[q['options'][i] for i in order]
  q['target']=[q['target'][i] for i in order]
  q['label']=max(range(len(order)),key=q['target'].__getitem__)
 return result

def check_groups(splits):
 seen={}
 for split,rows in splits.items():
  for row in rows:
   group=row.get('group_id')
   if not isinstance(group,str) or not group.strip():raise ValueError('Every row needs a semantic group_id')
   if group in seen and seen[group]!=split:raise ValueError('Semantic group crosses splits: '+group)
   seen[group]=split
