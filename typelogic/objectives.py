"""Proper scoring losses for human labels and teacher soft distributions."""
import torch
import torch.nn.functional as F

def decision_loss(logits,target,ordinal=False,ce_weight=1.,brier_weight=.2,ordinal_weight=.1,permuted_logits=None,permutation_weight=.1):
    if logits.shape!=target.shape or logits.numel()<2 or not torch.isfinite(logits).all() or not torch.isfinite(target).all() or (target<0).any() or not torch.allclose(target.sum(-1),torch.ones_like(target.sum(-1)),atol=1e-6):raise ValueError('Invalid training logits or targets')
    logp=F.log_softmax(logits.float(),dim=-1);p=logp.exp();t=target.float()
    loss=ce_weight*(-t*logp).sum(-1).mean()+brier_weight*((p-t)**2).sum(-1).mean()
    if ordinal:loss=loss+ordinal_weight*((p.cumsum(-1)[...,:-1]-t.cumsum(-1)[...,:-1])**2).mean()
    if permuted_logits is not None:
        # Caller aligns option IDs before passing the second logits vector.
        logq=F.log_softmax(permuted_logits.float(),dim=-1);q=logq.exp()
        loss=loss+permutation_weight*.5*((p*(logp-logq)).sum(-1)+(q*(logq-logp)).sum(-1)).mean()
    return loss
