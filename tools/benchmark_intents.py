"""CPU-only supervised intent baseline. Research tool; never loaded by offline runtime."""
import argparse
import hashlib
import json
import os
import platform
import sys
import time
import warnings
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from typelogic.learning import assert_disjoint, training_record, file_sha256
from typelogic.benchmark_metrics import metrics


def probabilities(logits,temperature=1.):
    import numpy as np
    if not np.isfinite(temperature) or temperature<=0:raise ValueError('Invalid temperature')
    z=np.asarray(logits,dtype=np.float64)/temperature
    if z.ndim!=2 or z.shape[1]<2 or not np.isfinite(z).all():raise ValueError('Invalid logits')
    p=np.exp(z-z.max(axis=1,keepdims=True));return p/p.sum(axis=1,keepdims=True)


def fit_temperature(logits,gold,labels):
    import numpy as np
    from scipy.optimize import minimize_scalar
    indexes=np.array([labels.index(g) for g in gold]);z=np.asarray(logits)
    def objective(log_t):
        p=probabilities(z,float(np.exp(log_t)))
        return -np.log(np.maximum(p[np.arange(len(gold)),indexes],1e-300)).mean()
    result=minimize_scalar(objective,bounds=(np.log(.05),np.log(20.)),method='bounded')
    return float(np.exp(result.x)) if result.success and objective(result.x)<objective(0.) else 1.


def vectorizer():
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.pipeline import FeatureUnion
    return FeatureUnion([('word',TfidfVectorizer(ngram_range=(1,2),max_features=20000,min_df=2,sublinear_tf=True)),
                         ('char',TfidfVectorizer(analyzer='char_wb',ngram_range=(3,5),max_features=20000,min_df=2,sublinear_tf=True))])


def select_source(splits,source):
    result={s:[r for r in rows if r['source_id']==source] for s,rows in splits.items()}
    if any(not rows for rows in result.values()):raise ValueError('Missing source or empty split: '+source)
    return result


def write_json(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,indent=2,ensure_ascii=False,allow_nan=False)


def evaluate(clf,x,rows,temperature=1.):
    z=clf.decision_function(x)
    # sklearn binary decision_function returns one signed margin.
    if z.ndim==1:
        import numpy as np
        z=np.column_stack([np.zeros_like(z),z])
    p=probabilities(z,temperature)
    return metrics(list(clf.classes_),[r['gold_label'] for r in rows],p.tolist()),z,p


def run(data_dir,out,sources=('banking77','clinc150'),threads=1):
    import numpy as np
    import scipy
    import sklearn
    from sklearn.linear_model import LogisticRegression
    from sklearn.exceptions import ConvergenceWarning
    from threadpoolctl import threadpool_limits,threadpool_info
    data_dir,out=Path(data_dir),Path(out)
    if out.exists():raise ValueError('Output exists; choose a new directory')
    if threads<1:raise ValueError('Threads must be positive')
    manifest=json.loads((data_dir/'manifest.json').read_text(encoding='utf-8'))
    splits={};hashes={}
    for split in ('train','validation','calibration','test'):
        path=data_dir/f'{split}.jsonl';hashes[split]=file_sha256(path)
        if hashes[split]!=manifest['files'][path.name]:raise ValueError('Dataset hash mismatch: '+split)
        splits[split]=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        for r in splits[split]:training_record(r)
    assert_disjoint(splits)
    out.mkdir(parents=True)
    config={'method':'tfidf_word_char_logistic','C_candidates':[1.,4.],'solver':'lbfgs','max_iter':300,'tol':1e-4,
            'seed':1701,'threads':threads,'feature_fit_split':'train','selection_split':'validation','selection_metric':'macro_f1_then_nll',
            'temperature_fit_split':'calibration','test_role':'historical_filtered_official_test','state_truncation':False,
            'feature_limits':{'word_1_2':20000,'char_wb_3_5':20000},'min_df':2,'sublinear_tf':True,
            'sources':list(sources),'dataset_sha256':hashes,'script_sha256':file_sha256(Path(__file__)),
            'metrics_sha256':file_sha256(ROOT/'typelogic/benchmark_metrics.py')}
    write_json(out/'protocol.json',config)
    reports={};start=time.perf_counter()
    with threadpool_limits(limits=threads):
        hardware={'platform':platform.platform(),'python':platform.python_version(),'cpu':platform.processor(),'logical_cpus':os.cpu_count(),
                  'numpy':np.__version__,'scipy':scipy.__version__,'sklearn':sklearn.__version__,'threadpools':threadpool_info()}
        for source in sources:
            rows=select_source(splits,source);catalog=sorted({r['gold_label'] for r in rows['train']})
            for items in rows.values():
                if any(set(r['questions']['intent']['criteria'])!=set(catalog) for r in items):raise ValueError('Catalog differs from trained classes')
                if any(r['language']!='en' or len(r['questions'])!=1 for r in items):raise ValueError('Only English single-intent sources are supported')
            print('Fitting '+source+' features on train only...',file=sys.stderr,flush=True)
            t0=time.perf_counter();features=vectorizer()
            x={'train':features.fit_transform([r['state'] for r in rows['train']])}
            for s in ('validation','calibration','test'):x[s]=features.transform([r['state'] for r in rows[s]])
            history=[];best=None;bestkey=None
            for c in config['C_candidates']:
                print(f'Fitting {source}, C={c}...',file=sys.stderr,flush=True)
                clf=LogisticRegression(C=c,max_iter=config['max_iter'],tol=config['tol'],solver='lbfgs',random_state=1701)
                with warnings.catch_warnings(record=True) as captured:
                    warnings.simplefilter('always',ConvergenceWarning)
                    clf.fit(x['train'],[r['gold_label'] for r in rows['train']])
                if any(isinstance(w.message,ConvergenceWarning) for w in captured):raise RuntimeError('Nonconverged baseline; no test results reported')
                val,_,_=evaluate(clf,x['validation'],rows['validation'])
                history.append({'C':c,'validation_accuracy':val['accuracy'],'validation_macro_f1':val['macro_f1'],'validation_nll':val['nll'],'iterations':clf.n_iter_.tolist()})
                key=(val['macro_f1'],-val['nll'])
                if bestkey is None or key>bestkey:best,bestkey=clf,key
            labels=list(best.classes_);_,calz,_=evaluate(best,x['calibration'],rows['calibration'])
            temperature=fit_temperature(calz,[r['gold_label'] for r in rows['calibration']],labels)
            result={'counts':{s:len(v) for s,v in rows.items()},'features':x['train'].shape[1],'selection':history,'selected_C':best.C,'temperature':temperature,
                    'temperature_boundary':temperature<.051 or temperature>19.9,'training_and_selection_seconds':time.perf_counter()-t0,'splits':{}}
            dest=out/source;dest.mkdir()
            for s in ('validation','calibration','test'):
                raw,z,p=evaluate(best,x[s],rows[s]);calp=probabilities(z,temperature)
                result['splits'][s]={'raw':raw,'calibrated':metrics(labels,[r['gold_label'] for r in rows[s]],calp.tolist())}
                # Numeric logits permit alternate evaluators to reproduce the exact probabilities.
                with (dest/f'{s}-logits.npz').open('xb') as archive:
                    np.savez_compressed(archive,logits=z,labels=np.asarray(labels),ids=np.asarray([r['id'] for r in rows[s]]))
                with np.load(dest/f'{s}-logits.npz',allow_pickle=False) as saved:
                    if not np.array_equal(saved['logits'],z):raise RuntimeError('Logit archive verification failed')
                with (dest/f'{s}-predictions.jsonl').open('x',encoding='utf-8') as f:
                    for r,prob in zip(rows[s],calp):
                        value={'id':r['id'],'source_id':source,'split':s,'state_sha256':hashlib.sha256(r['state'].encode()).hexdigest(),'gold':r['gold_label'],
                               'question_sha256':hashlib.sha256(json.dumps(r['questions'],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),
                               'method':config['method'],'temperature':temperature,'probabilities':dict(zip(labels,map(float,prob)))}
                        f.write(json.dumps(value,separators=(',',':'),ensure_ascii=False)+'\n')
            # One cold transform/predict, one warm-up and a deterministic sample at batch size one.
            sample=sorted(rows['test'],key=lambda r:hashlib.sha256(r['id'].encode()).hexdigest())[:200]
            t=time.perf_counter();best.predict_proba(features.transform([sample[0]['state']]));cold=time.perf_counter()-t
            timings=[]
            for r in sample:
                t=time.perf_counter();zz=best.decision_function(features.transform([r['state']]));probabilities(zz,temperature);timings.append(time.perf_counter()-t)
            result['latency']={'unit':'seconds','batch_size':1,'sample_questions':len(sample),'p50':float(np.percentile(timings,50)),'p95':float(np.percentile(timings,95)),
                               'first_probe_seconds':cold,'includes':'TF-IDF transform + linear logits + temperature softmax; excludes loading, JSON and app overhead','sample_ids':[r['id'] for r in sample]}
            result['predictive_numeric_bytes']=sum(v.idf_.nbytes for _,v in features.transformer_list)+best.coef_.nbytes+best.intercept_.nbytes
            result['numeric_size_note']='Excludes vocabulary objects, process RAM, temporary matrices and Python runtime'
            reports[source]=result;write_json(dest/'report.json',result)
            print(json.dumps({'source':source,'test_accuracy':result['splits']['test']['raw']['accuracy'],'C':best.C,'temperature':temperature}),file=sys.stderr,flush=True)
    report={'protocol':config,'environment':hardware,'results':reports,'seconds':time.perf_counter()-start,'trained_qwen':False,'deployment_eligible':False,
            'limitations':['English fixed-catalog classification only','Historical filtered test, not certified unseen','No semantic-family leakage certification','Not a paired comparison with Qwen/JEV/Laya/Llama','Latency measured on this Linux CPU, not the user Windows CPU','Classifiers are not installed in the application']}
    try:
        import resource
        report['process_peak_rss_mib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024 if sys.platform!='darwin' else 1024**2)
    except ImportError:report['process_peak_rss_mib']=None
    write_json(out/'report.json',report)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,default=ROOT/'research/data/intent-choice-en-v2')
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--source',choices=('banking77','clinc150'),action='append')
    p.add_argument('--threads',type=int,default=1)
    a=p.parse_args();r=run(a.data_dir,a.out,tuple(dict.fromkeys(a.source or ['banking77','clinc150'])),a.threads)
    print(json.dumps({'report':str(a.out/'report.json'),'seconds':r['seconds']},indent=2))

if __name__=='__main__':main()
