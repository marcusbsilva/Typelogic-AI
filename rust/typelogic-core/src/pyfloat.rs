//! `repr(float)` exactly as CPython prints it (shortest round-trip digits, 'r' format rules).

/// Shortest round-tripping digits in `d.ddde±x` form, chosen like CPython's dtoa mode 0: the fewest digits,
/// then the candidate closest to `x`, ties to even. `{:.Ne}` is exact and rounds half to even.
fn shortest_digits(x: f64) -> String {
    for n in 1..=17usize {
        let s = format!("{:.*e}", n - 1, x);
        if s.parse::<f64>().ok() == Some(x) {
            return s;
        }
        // Near powers of two the round-trip interval is asymmetric: a neighbour of the correctly
        // rounded n-digit value may round-trip when the value itself does not.
        let (m, e) = s.split_once('e').unwrap();
        let digits: String = m.chars().filter(|c| *c != '.').collect();
        let base: u64 = digits.parse().unwrap();
        let exp: i32 = e.parse().unwrap();
        let mut best: Option<(f64, String)> = None;
        for cand in [base.wrapping_sub(1), base + 1] {
            let mut cd = cand.to_string();
            let mut ce = exp;
            if cd.len() > n {
                cd.truncate(n);
                ce += 1;
            } else if cd.len() < n {
                if cd == "0" { continue; }
                cd.push('0');
                ce -= 1;
            }
            let text = if n == 1 { format!("{}e{}", cd, ce) } else { format!("{}.{}e{}", &cd[..1], &cd[1..], ce) };
            if let Ok(v) = text.parse::<f64>() {
                if v == x {
                    let dist = (v - x).abs();
                    if best.as_ref().map(|(d, _)| dist < *d).unwrap_or(true) {
                        best = Some((dist, text));
                    }
                }
            }
        }
        if let Some((_, text)) = best {
            return text;
        }
    }
    format!("{:e}", x)
}

/// Format a finite or non-finite `f64` the way Python's `float.__repr__` does.
pub fn repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    let sci = shortest_digits(x.abs());
    let (mantissa, exponent) = sci.split_once('e').expect("scientific format");
    let exponent: i32 = exponent.parse().expect("exponent");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    // decpt: position of the decimal point relative to the start of `digits` (value = 0.d1d2.. * 10^decpt).
    let decpt = exponent + 1;
    let mut out = String::new();
    if x.is_sign_negative() {
        out.push('-');
    }
    if decpt <= -4 || decpt > 16 {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let e = decpt - 1;
        out.push('e');
        out.push(if e < 0 { '-' } else { '+' });
        out.push_str(&format!("{:02}", e.abs()));
    } else if decpt <= 0 {
        out.push_str("0.");
        for _ in 0..(-decpt) {
            out.push('0');
        }
        out.push_str(&digits);
    } else {
        let d = decpt as usize;
        if digits.len() <= d {
            out.push_str(&digits);
            for _ in digits.len()..d {
                out.push('0');
            }
            out.push_str(".0");
        } else {
            out.push_str(&digits[..d]);
            out.push('.');
            out.push_str(&digits[d..]);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::repr;

    #[test]
    fn matches_cpython_examples() {
        let cases = [
            (1e16, "1e+16"), (1e15, "1000000000000000.0"), (123456789012345678.0, "1.2345678901234568e+17"),
            (0.0001, "0.0001"), (0.00001, "1e-05"), (1.5e-5, "1.5e-05"), (-0.0, "-0.0"), (1.0, "1.0"), (2.5, "2.5"),
            (1e22, "1e+22"), (f64::MAX, "1.7976931348623157e+308"), (5e-324, "5e-324"),
            (0.1 + 0.2, "0.30000000000000004"), (100.0, "100.0"), (1e-7, "1e-07"), (123.456e10, "1234560000000.0"),
            (-12.5, "-12.5"), (0.5, "0.5"),
        ];
        for (x, want) in cases {
            assert_eq!(repr(x), want, "{x}");
        }
    }
}
