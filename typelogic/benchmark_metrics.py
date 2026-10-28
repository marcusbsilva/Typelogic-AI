"""Finite, ID-aligned metrics shared by fixed-catalog intent benchmarks."""
import math
from collections import Counter


def align_probabilities(values, labels):
    if len(labels)<2 or len(set(labels))!=len(labels) or set(values)!=set(labels):
        raise ValueError('Probability IDs must exactly match the catalog')
    row=[values[k] for k in labels]
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<0 for x in row):
        raise ValueError('Invalid probability')
    total=math.fsum(row)
    if abs(total-1)>1e-6:raise ValueError('Probabilities must sum to one')
    return [x/total for x in row]


def metrics(labels,gold,probabilities):
    if not gold or len(gold)!=len(probabilities):raise ValueError('Empty or inconsistent evaluation')
    if len(set(labels))!=len(labels) or len(labels)<2:raise ValueError('Invalid catalog')
    lookup={k:i for i,k in enumerate(labels)}
    if any(g not in lookup for g in gold):raise ValueError('Unknown gold label')
    n=len(gold);correct=0;nll=0.;brier=0.;confusion=Counter();bins=[[] for _ in range(10)];scored=[]
    for g,values in zip(gold,probabilities):
        if len(values)!=len(labels):raise ValueError('Invalid probability dimension')
        p=align_probabilities(dict(zip(labels,values)),labels)
        best=max(range(len(p)),key=lambda i:(p[i],-i));pred=labels[best];hit=int(pred==g)
        correct+=hit;nll-=math.log(max(p[lookup[g]],1e-300))
        brier+=math.fsum((v-float(i==lookup[g]))**2 for i,v in enumerate(p))
        confusion[g,pred]+=1;confidence=p[best];bins[min(9,int(confidence*10))].append((confidence,hit));scored.append((confidence,hit))
    per_class={};f1=[]
    for label in labels:
        tp=confusion[label,label];actual=sum(v for (g,_),v in confusion.items() if g==label);predicted=sum(v for (_,p),v in confusion.items() if p==label)
        value=2*tp/(actual+predicted) if actual+predicted else 0.;f1.append(value)
        per_class[label]={'support':actual,'precision':tp/predicted if predicted else 0.,'recall':tp/actual if actual else 0.,'f1':value}
    selective={}
    for threshold in (.5,.7,.85,.9,.95):
        accepted=[h for c,h in scored if c>=threshold]
        selective[str(threshold)]={'accepted':len(accepted),'coverage':len(accepted)/n,'accuracy':sum(accepted)/len(accepted) if accepted else None,'error_rate':1-sum(accepted)/len(accepted) if accepted else None}
    return {'questions':n,'correct':correct,'accuracy':correct/n,'macro_f1':math.fsum(f1)/len(labels),'nll':nll/n,'brier':brier/n,
            'ece_10_bins':sum(abs(sum(c for c,_ in bucket)-sum(h for _,h in bucket)) for bucket in bins)/n,
            'selective':selective,'per_class':per_class,'confusion':[{'gold':g,'predicted':p,'count':v} for (g,p),v in sorted(confusion.items())]}


def paired_comparison(left,right):
    a={r['id']:r for r in left};b={r['id']:r for r in right}
    if not a or set(a)!=set(b) or len(a)!=len(left) or len(b)!=len(right):raise ValueError('Require exactly the same unique case IDs')
    wins=losses=both=neither=0
    for key in sorted(a):
        x,y=a[key],b[key]
        if (x['state_sha256'],x['question_sha256'],x['gold'])!=(y['state_sha256'],y['question_sha256'],y['gold']):raise ValueError('Mismatched case content or gold')
        labels=sorted(x['probabilities'])
        px=align_probabilities(x['probabilities'],labels);py=align_probabilities(y['probabilities'],labels)
        if x['gold'] not in labels:raise ValueError('Unknown gold')
        hx=labels[max(range(len(labels)),key=px.__getitem__)]==x['gold']
        hy=labels[max(range(len(labels)),key=py.__getitem__)]==x['gold']
        wins+=hx and not hy;losses+=hy and not hx;both+=hx and hy;neither+=not hx and not hy
    discord=wins+losses
    # Exact two-sided binomial McNemar test. Independence between cases is an assumption.
    p=min(1.,2*sum(math.comb(discord,k) for k in range(min(wins,losses)+1))/(2**discord)) if discord else 1.
    return {'questions':len(a),'left_only_correct':wins,'right_only_correct':losses,'both_correct':both,'both_wrong':neither,
            'accuracy_delta_left_minus_right':(wins-losses)/len(a),'mcnemar_exact_p':p,'warning':'Paired case equality does not establish equal training budgets, semantic independence or unseen-test status.'}
