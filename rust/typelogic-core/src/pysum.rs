//! CPython's built-in `sum()` over floats (3.12+): an int start of 0, then Neumaier-compensated summation.
//! Reproducing it bit-for-bit keeps normalized probabilities identical to the Python reference.

/// `sum(values)` for an iterator of floats with the default start `0`.
pub fn py_sum<I: IntoIterator<Item = f64>>(values: I) -> f64 {
    let mut iter = values.into_iter();
    let Some(first) = iter.next() else { return 0.0 };
    // int 0 + float x: converts 0 to 0.0, so -0.0 becomes 0.0.
    let mut f = 0.0 + first;
    let mut c = 0.0f64;
    for x in iter {
        let t = f + x;
        if f.abs() >= x.abs() {
            c += (f - t) + x;
        } else {
            c += (x - t) + f;
        }
        f = t;
    }
    // Avoid losing the sign of a negative result and never turn inf into nan.
    if c != 0.0 && c.is_finite() {
        f += c;
    }
    f
}

#[cfg(test)]
mod tests {
    use super::py_sum;

    #[test]
    fn compensated_like_cpython() {
        assert_eq!(py_sum([0.1; 10]), 1.0); // naive summation gives 0.9999999999999999
        assert_eq!(py_sum([1e100, 1.0, -1e100]), 1.0);
        assert_eq!(py_sum(Vec::<f64>::new()), 0.0);
        assert!(py_sum([-0.0]).is_sign_positive());
    }
}
