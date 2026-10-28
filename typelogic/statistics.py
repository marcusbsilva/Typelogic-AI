"""Uncertainty estimates for measured finite classification samples."""
import math,random

def wilson(correct,total,z=1.959963984540054):
    if isinstance(total,bool) or not isinstance(total,int) or total<1 or not isinstance(correct,int) or not 0<=correct<=total:raise ValueError('Invalid binomial sample')
    p=correct/total;d=1+z*z/total;center=(p+z*z/(2*total))/d;radius=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/d
    return [max(0,center-radius),min(1,center+radius)]

def paired_bootstrap(a,b,iterations=2000,seed=1701):
    if len(a)!=len(b) or not a or not 100<=iterations<=100000:raise ValueError('Invalid paired sample')
    n=len(a);rng=random.Random(seed);difference=[]
    for _ in range(iterations):
        indices=[rng.randrange(n) for _ in range(n)];difference.append(sum(int(a[i])-int(b[i]) for i in indices)/n)
    difference.sort();return {'difference':sum(int(x)-int(y) for x,y in zip(a,b))/n,'ci95_percentile':[difference[int(.025*iterations)],difference[min(iterations-1,int(.975*iterations))]],'iterations':iterations,'seed':seed,'unit':'case','warning':'Descriptive sample uncertainty; public pretraining contamination remains unknown.'}
