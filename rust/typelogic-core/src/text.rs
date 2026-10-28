//! Python `str` semantics needed by the contracts: whitespace, `\w`, `\d` and code-point length.
use crate::unicode_tables::{PY_DECIMAL, PY_DECIMAL_ZEROS, PY_SPACE, PY_WORD};

fn in_table(table: &[(u32, u32)], c: char) -> bool {
    let cp = c as u32;
    table
        .binary_search_by(|&(lo, hi)| {
            if hi < cp {
                std::cmp::Ordering::Less
            } else if lo > cp {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// `str.isspace()` for one character.
pub fn is_space(c: char) -> bool { in_table(PY_SPACE, c) }

/// `re` Unicode `\w`: `str.isalnum()` or underscore.
pub fn is_word(c: char) -> bool { in_table(PY_WORD, c) }

/// `re` Unicode `\d` / `str.isdecimal()`.
pub fn is_decimal(c: char) -> bool { in_table(PY_DECIMAL, c) }

/// `unicodedata.decimal(c)` for a decimal digit.
pub fn decimal_value(c: char) -> Option<u32> {
    if !is_decimal(c) {
        return None;
    }
    let cp = c as u32;
    let idx = PY_DECIMAL_ZEROS.partition_point(|&z| z <= cp);
    let zero = PY_DECIMAL_ZEROS[idx - 1];
    Some(cp - zero)
}

/// `s.strip()` (no arguments).
pub fn strip(s: &str) -> &str { s.trim_matches(is_space) }

/// `len(s)` in code points.
pub fn py_len(s: &str) -> usize { s.chars().count() }

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_whitespace_includes_information_separators() {
        assert!(is_space('\u{1c}') && is_space('\u{85}') && is_space('\u{3000}'));
        assert!(!is_space('\u{200b}'));
        assert_eq!(strip("\u{1c} x \u{a0}"), "x");
    }

    #[test]
    fn decimal_digits_include_other_scripts() {
        assert_eq!(decimal_value('7'), Some(7));
        assert_eq!(decimal_value('\u{663}'), Some(3)); // ARABIC-INDIC DIGIT THREE
        assert_eq!(decimal_value('x'), None);
        assert!(is_word('_') && is_word('é') && !is_word('-'));
    }
}
