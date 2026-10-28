"""Dataset metrics; failed responses count against global accuracy."""
import math
from collections import Counter

def summary(rows):
    if not rows:raise ValueError('No evaluated questions')
    valid=[r for r in rows if r.get('probabilities') is not None];accepted=[r for r in valid if r.get('candidate',False)]
    correct=lambda r:r['keys'][max(range(len(r['probabilities'])),key=r['probabilities'].__getitem__)]==r['keys'][max(range(len(r['target'])),key=r['target'].__getitem__)]
    bins=[]
    for low in range(10):
        bucket=[r for r in valid if low/10<=max(r['probabilities'])<=1 and (max(r['probabilities'])<(low+1)/10 or low==9)]
        if bucket:bins.append({'lower':low/10,'upper':(low+1)/10,'n':len(bucket),'mean_probability':sum(max(r['probabilities']) for r in bucket)/len(bucket),'accuracy':sum(correct(r) for r in bucket)/len(bucket)})
    confusion=Counter()
    for r in rows:
        actual=r['keys'][max(range(len(r['target'])),key=r['target'].__getitem__)];predicted='<failure>' if r.get('probabilities') is None else r['keys'][max(range(len(r['probabilities'])),key=r['probabilities'].__getitem__)]
        confusion[(r.get('question_id',''),actual,predicted)]+=1
    nll=sum(-sum(t*math.log(max(p,1e-30)) for p,t in zip(r['probabilities'],r['target'])) for r in valid)/len(valid) if valid else None
    brier=sum(sum((p-t)**2 for p,t in zip(r['probabilities'],r['target'])) for r in valid)/len(valid) if valid else None
    accuracy=sum(correct(r) for r in valid)/len(rows);chance=sum(1/len(r['keys']) for r in rows)/len(rows)
    return {'questions':len(rows),'responded':len(valid),'failures':len(rows)-len(valid),'accuracy_including_failures':accuracy,
            'chance_expected_accuracy':chance,'chance_adjusted_accuracy':(accuracy-chance)/(1-chance),
            'nll_responded_only':nll,'brier_responded_only':brier,'ece_responded_only':sum(b['n']*abs(b['mean_probability']-b['accuracy']) for b in bins)/len(valid) if valid else None,
            'calibration_bins':bins,'candidate_coverage':len(accepted)/len(rows),'candidate_error_rate':1-sum(correct(r) for r in accepted)/len(accepted) if accepted else None,
            'abstention_rate':sum(bool(r.get('abstained')) for r in valid)/len(rows),'mandatory_human_review_rate':1.,
            'confusion':[{'question_id':q,'actual':a,'predicted':p,'count':n} for (q,a,p),n in sorted(confusion.items())]}
