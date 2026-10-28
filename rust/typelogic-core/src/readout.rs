//! Pointer readout of the trained decision head: `softmax((K(h_opts) · Q(h_decide)) * scale / T)`.
//! Same computation as `vendor.pointer.PointerHead.forward` followed by softmax; used as a dependency-free
//! alternative to the ONNX Runtime readout, accepted only after numerical parity with the PyTorch head.
use crate::error::{CoreError, Result};

#[derive(Debug, Clone)]
pub struct PointerHead {
    /// Input (hidden) size `d`.
    pub d: usize,
    /// Pointer size `dp`.
    pub dp: usize,
    /// `q.weight` as [dp, d] row-major (PyTorch `nn.Linear` layout).
    pub qw: Vec<f32>,
    pub qb: Vec<f32>,
    pub kw: Vec<f32>,
    pub kb: Vec<f32>,
    pub scale: f64,
    pub temperature: f64,
}

fn linear(w: &[f32], b: &[f32], x: &[f32], dp: usize, d: usize) -> Vec<f64> {
    let mut out = vec![0f64; dp];
    for (o, slot) in out.iter_mut().enumerate() {
        let row = &w[o * d..(o + 1) * d];
        let mut acc = 0f64;
        for (wi, xi) in row.iter().zip(x) {
            acc += *wi as f64 * *xi as f64;
        }
        *slot = acc + b[o] as f64;
    }
    out
}

impl PointerHead {
    pub fn new(d: usize, dp: usize, qw: Vec<f32>, qb: Vec<f32>, kw: Vec<f32>, kb: Vec<f32>, temperature: f64) -> Result<Self> {
        if d == 0 || dp == 0 || qw.len() != d * dp || kw.len() != d * dp || qb.len() != dp || kb.len() != dp {
            return Err(CoreError::value("Pointer head tensor shapes do not match"));
        }
        if !(temperature.is_finite() && temperature > 0.0 && temperature < 100.0) {
            return Err(CoreError::value("Invalid temperature"));
        }
        if qw.iter().chain(&qb).chain(&kw).chain(&kb).any(|v| !v.is_finite()) {
            return Err(CoreError::value("Pointer head contains non-finite weights"));
        }
        Ok(Self { d, dp, qw, qb, kw, kb, scale: 1.0 / (dp as f64).sqrt(), temperature })
    }

    /// Logits for one question: `decision` has `d` values, `options` is K×d row-major.
    pub fn logits(&self, decision: &[f32], options: &[f32]) -> Result<Vec<f64>> {
        if decision.len() != self.d || options.is_empty() || options.len() % self.d != 0 {
            return Err(CoreError::value("Readout input shapes do not match the head"));
        }
        let q = linear(&self.qw, &self.qb, decision, self.dp, self.d);
        // (Kw·o + kb)·q = o·(Kwᵀq) + kb·q: project the query once instead of every option.
        let mut u = vec![0f64; self.d];
        for (o, qo) in q.iter().enumerate() {
            let row = &self.kw[o * self.d..(o + 1) * self.d];
            for (uj, w) in u.iter_mut().zip(row) {
                *uj += *w as f64 * qo;
            }
        }
        let bias: f64 = self.kb.iter().zip(&q).map(|(b, qo)| *b as f64 * qo).sum();
        let mut z = Vec::with_capacity(options.len() / self.d);
        for opt in options.chunks_exact(self.d) {
            let dot: f64 = opt.iter().zip(&u).map(|(x, uj)| *x as f64 * uj).sum();
            z.push((dot + bias) * self.scale / self.temperature);
        }
        Ok(z)
    }

    pub fn probabilities(&self, decision: &[f32], options: &[f32]) -> Result<Vec<f64>> { Ok(softmax(&self.logits(decision, options)?)) }
}

pub fn softmax(z: &[f64]) -> Vec<f64> {
    let m = z.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let e: Vec<f64> = z.iter().map(|v| (v - m).exp()).collect();
    let s: f64 = e.iter().sum();
    e.into_iter().map(|v| v / s).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identity_head() {
        // d = dp = 2, q = identity, k = identity: logits = <opt, dec> / sqrt(2)
        let eye = vec![1.0, 0.0, 0.0, 1.0];
        let head = PointerHead::new(2, 2, eye.clone(), vec![0.0; 2], eye, vec![0.0; 2], 1.0).unwrap();
        let z = head.logits(&[1.0, 2.0], &[1.0, 0.0, 0.0, 1.0]).unwrap();
        assert!((z[0] - 1.0 / 2f64.sqrt()).abs() < 1e-12 && (z[1] - 2.0 / 2f64.sqrt()).abs() < 1e-12);
        let p = head.probabilities(&[1.0, 2.0], &[1.0, 0.0, 0.0, 1.0]).unwrap();
        assert!((p.iter().sum::<f64>() - 1.0).abs() < 1e-12 && p[1] > p[0]);
    }
}
