//! Evidence extraction (port of `typelogic/evidence.py`) and the evidence check of `typelogic/needle.py`.
//!
//! The generalist (Plumb, through llama-server) fills the evidence schema of a case from its free text, with JSON
//! constrained to the schema. Every value comes back unverified, with a check: written in the text, not found,
//! inferred or not stated. The values are suggestions: they enter a decision only after the user keeps them.
use crate::casefmt::{self, iso_date, EVIDENCE_TYPES};
use typelogic_core::json::{Map, Value};
use typelogic_core::text::{decimal_value, is_decimal, is_space, is_word, py_len};

pub const MAX_TEXT_CHARS: usize = 60000;
pub const MAX_CONTEXT: usize = 8192 - 16;
pub const SYSTEM: &str = "You extract structured evidence for a decision review. Fill each field with the value stated in the text. \
                          Copy numbers, dates (YYYY-MM-DD), names and codes exactly as written. Use null when the text does not state \
                          the value. Never compute, add, convert or guess a value, and never answer the decision itself.";
pub const NOTE: &str = "Suggested by a local model. Unverified until you keep or correct each value.";

const MONTHS: [&[&str]; 12] = [
    &["january", "jan", "janeiro", "enero"],
    &["february", "feb", "fevereiro", "febrero", "fev"],
    &["march", "mar", "março", "marco", "marzo"],
    &["april", "apr", "abril", "abr"],
    &["may", "maio", "mayo", "mai"],
    &["june", "jun", "junho", "junio"],
    &["july", "jul", "julho", "julio"],
    &["august", "aug", "agosto", "ago"],
    &["september", "sep", "sept", "setembro", "septiembre", "set"],
    &["october", "oct", "outubro", "octubre", "out"],
    &["november", "nov", "novembro", "noviembre"],
    &["december", "dec", "dezembro", "diciembre", "dez", "dic"],
];

const NUMBER_WORDS: [(&str, u32); 30] = [
    ("zero", 0), ("nenhum", 0), ("nenhuma", 0), ("cero", 0), ("one", 1), ("two", 2), ("dois", 2), ("duas", 2), ("three", 3), ("três", 3),
    ("tres", 3), ("four", 4), ("quatro", 4), ("cuatro", 4), ("five", 5), ("cinco", 5), ("six", 6), ("seis", 6), ("seven", 7), ("sete", 7),
    ("siete", 7), ("eight", 8), ("oito", 8), ("ocho", 8), ("nine", 9), ("nove", 9), ("nueve", 9), ("ten", 10), ("dez", 10), ("diez", 10),
];
const NUMBER_WORDS_2: [(&str, u32); 6] = [("eleven", 11), ("onze", 11), ("once", 11), ("twelve", 12), ("doze", 12), ("doce", 12)];

/// `str.casefold()` for the characters that differ from lowercase in common text.
fn casefold(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            'ß' | 'ẞ' => out.push_str("ss"),
            'ς' => out.push('σ'),
            'ﬀ' => out.push_str("ff"),
            'ﬁ' => out.push_str("fi"),
            'ﬂ' => out.push_str("fl"),
            'ﬃ' => out.push_str("ffi"),
            'ﬄ' => out.push_str("ffl"),
            'ﬅ' | 'ﬆ' => out.push_str("st"),
            'ŉ' => out.push_str("ʼn"),
            'ſ' => out.push('s'),
            'µ' => out.push('μ'),
            c => out.extend(c.to_lowercase()),
        }
    }
    out
}

/// `' '.join(str(text).casefold().split())`
fn norm(text: &str) -> String { casefold(text).split(is_space).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ") }

/// `float(s)` for the digit strings the number pattern finds (other scripts' digits included), else None.
fn py_float(s: &str) -> Option<f64> {
    let ascii: String = s.chars().map(|c| decimal_value(c).map(|d| char::from(b'0' + d as u8)).unwrap_or(c)).collect();
    let body = ascii.strip_prefix(['+', '-']).unwrap_or(&ascii);
    if body.is_empty() || body.starts_with('.') && body.len() == 1 || !body.chars().all(|c| c.is_ascii_digit() || c == '.') || body.matches('.').count() > 1 {
        return None;
    }
    ascii.parse().ok()
}

/// Every number written in `text` (`needle._numbers`): digits with dot or comma decimals and thousands separators,
/// and small numbers written as words before a noun ("zero units", "duas vezes").
pub fn numbers(text: &str) -> Vec<f64> {
    let c: Vec<char> = text.chars().collect();
    let mut values = Vec::new();
    // _COUNTED: \b(word)\s+([^\W\d_]+), case-insensitive, longest word first
    let lower: Vec<char> = c.iter().map(|ch| ch.to_lowercase().next().unwrap_or(*ch)).collect();
    let mut i = 0;
    while i < c.len() {
        let boundary = (i == 0 || !is_word(c[i - 1])) && is_word(c[i]);
        let mut matched = None;
        if boundary {
            let mut words: Vec<(&str, u32)> = NUMBER_WORDS.iter().chain(NUMBER_WORDS_2.iter()).copied().collect();
            words.sort_by_key(|w| std::cmp::Reverse(w.0.chars().count()));
            for (w, v) in words {
                let n = w.chars().count();
                if i + n > c.len() || !lower[i..i + n].iter().copied().eq(w.chars()) {
                    continue;
                }
                let mut j = i + n;
                if j >= c.len() || !is_space(c[j]) {
                    continue;
                }
                while j < c.len() && is_space(c[j]) {
                    j += 1;
                }
                let start = j;
                while j < c.len() && is_word(c[j]) && !is_decimal(c[j]) && c[j] != '_' {
                    j += 1;
                }
                if j > start {
                    matched = Some((j, v));
                    break;
                }
            }
        }
        match matched {
            Some((j, v)) => {
                values.push(v as f64);
                i = j;
            }
            None => i += 1,
        }
    }
    // _NUMBER: (?<![\w.,])[-+]?\d[\d.,  ]*\d(?![\w]) | (?<![\w.,])[-+]?\d(?![\w])
    let in_class = |ch: char| is_decimal(ch) || matches!(ch, '.' | ',' | ' ' | '\u{a0}' | '\u{202f}');
    let mut p = 0;
    while p < c.len() {
        let behind_ok = p == 0 || !(is_word(c[p - 1]) || c[p - 1] == '.' || c[p - 1] == ',');
        let mut found = None;
        if behind_ok {
            let q = if matches!(c[p], '+' | '-') { p + 1 } else { p };
            if q < c.len() && is_decimal(c[q]) {
                let mut r = q + 1;
                while r < c.len() && in_class(c[r]) {
                    r += 1;
                }
                let mut e = r;
                while e >= q + 2 {
                    if is_decimal(c[e - 1]) && (e == c.len() || !is_word(c[e])) {
                        found = Some(e);
                        break;
                    }
                    e -= 1;
                }
                if found.is_none() && (q + 1 == c.len() || !is_word(c[q + 1])) {
                    found = Some(q + 1);
                }
            }
        }
        let Some(end) = found else {
            p += 1;
            continue;
        };
        let raw: String = c[p..end].iter().filter(|ch| !matches!(ch, ' ' | '\u{a0}' | '\u{202f}')).collect();
        let raw = raw.trim_end_matches(['.', ',']).to_owned();
        let mut candidates: Vec<String> = Vec::new();
        let (has_c, has_d) = (raw.contains(','), raw.contains('.'));
        if has_c && has_d {
            candidates.push(if raw.rfind('.') > raw.rfind(',') { raw.replace(',', "") } else { raw.replace('.', "").replace(',', ".") });
        } else if has_c {
            if raw.matches(',').count() == 1 {
                candidates.push(raw.replace(',', ""));
                candidates.push(raw.replace(',', "."));
            } else {
                candidates.push(raw.replace(',', ""));
            }
        } else if raw.matches('.').count() > 1 {
            candidates.push(raw.replace('.', ""));
        } else if has_d {
            candidates.push(raw.clone());
            candidates.push(raw.replace('.', ""));
        } else {
            candidates.push(raw.clone());
        }
        values.extend(candidates.iter().filter_map(|s| py_float(s)));
        p = end;
    }
    values
}

/// `(?<!\d)<pattern>(?!\d)` search over chars, where `pat` returns the end of a match starting at i.
fn search(c: &[char], pat: impl Fn(usize) -> Option<usize>) -> bool {
    (0..c.len()).any(|i| (i == 0 || !is_decimal(c[i - 1])) && pat(i).is_some_and(|e| e == c.len() || !is_decimal(c[e])))
}

/// `0?{n}` at i: the end after the number (with at most one leading zero), or None.
fn number_at(c: &[char], i: usize, n: u32) -> Option<usize> {
    let digits = n.to_string();
    let ends: Vec<usize> = [0usize, 1].iter().filter_map(|&z| {
        let mut j = i;
        if z == 1 {
            if c.get(j) != Some(&'0') {
                return None;
            }
            j += 1;
        }
        for d in digits.chars() {
            if c.get(j) != Some(&d) {
                return None;
            }
            j += 1;
        }
        Some(j)
    }).collect();
    ends.into_iter().next_back()
}

/// `needle._date_in_text(value, text)`: ISO, day/month/year in digits, or day, month name and year.
fn date_in_text(value: &str, text: &str) -> bool {
    let Some((year, month, day)) = iso_date(value) else { return false };
    if text.contains(value) {
        return true;
    }
    let low: Vec<char> = norm(text).chars().collect();
    let yy = format!("{:02}", year.rem_euclid(100));
    let sep = |ch: Option<&char>| matches!(ch, Some('/' | '.' | '-'));
    let year_at = |i: usize| -> Option<usize> {
        // (\d\d)?YY, longest first
        let two = |j: usize| (j + 2 <= low.len() && low[j..j + 2].iter().copied().eq(yy.chars())).then_some(j + 2);
        if i + 2 <= low.len() && is_decimal(low[i]) && is_decimal(low[i + 1]) {
            if let Some(e) = two(i + 2) {
                return Some(e);
            }
        }
        two(i)
    };
    for (first, second) in [(day, month), (month, day)] {
        let found = search(&low, |i| {
            let a = number_at(&low, i, first)?;
            if !sep(low.get(a)) {
                return None;
            }
            let b = number_at(&low, a + 1, second)?;
            if !sep(low.get(b)) {
                return None;
            }
            year_at(b + 1)
        });
        if found {
            return true;
        }
    }
    // words: [^\W\d_]+ runs
    let mut words = Vec::new();
    let mut cur = String::new();
    for &ch in &low {
        if is_word(ch) && !is_decimal(ch) && ch != '_' {
            cur.push(ch);
        } else if !cur.is_empty() {
            words.push(std::mem::take(&mut cur));
        }
    }
    if !cur.is_empty() {
        words.push(cur);
    }
    let named = MONTHS[(month - 1) as usize].iter().any(|m| words.iter().any(|w| w == m));
    let written_day = search(&low, |i| {
        let e = number_at(&low, i, day)?;
        for suffix in ["st", "nd", "rd", "th", "º"] {
            let n = suffix.chars().count();
            if e + n <= low.len() && low[e..e + n].iter().copied().eq(suffix.chars()) && (e + n == low.len() || !is_decimal(low[e + n])) {
                return Some(e + n);
            }
        }
        Some(e)
    });
    let ys = year.to_string();
    let year4 = search(&low, |i| (i + ys.len() <= low.len() && low[i..i + ys.len()].iter().copied().eq(ys.chars())).then_some(i + ys.len()));
    named && written_day && year4
}

/// `needle.evidence(value, kind, text)`: 'in_text', 'not_in_text' or 'inferred' (yes/no fields).
pub fn check(value: &Value, kind: &str, text: &str) -> &'static str {
    match kind {
        "boolean" => "inferred",
        "number" | "integer" => {
            let v = match value {
                Value::Int(_) | Value::Float(_) => casefmt::number(value).filter(|f| f.is_finite()),
                _ => None,
            };
            match v {
                Some(v) if numbers(text).iter().any(|n| (n - v).abs() <= 1e-9 * v.abs().max(1.0)) => "in_text",
                _ => "not_in_text",
            }
        }
        "date" => match value.as_str() {
            Some(s) if date_in_text(s, text) => "in_text",
            _ => "not_in_text",
        },
        _ => match value.as_str() {
            Some(s) if !typelogic_core::text::strip(s).is_empty() && norm(text).contains(&norm(s)) => "in_text",
            _ => "not_in_text",
        },
    }
}

/// The check of an extracted value (`evidence._check`): enums and yes/no fields are inferred unless written.
pub fn field_check(value: &Value, kind: &str, text: &str) -> &'static str {
    match kind {
        "boolean" => "inferred",
        "enum" => if value.as_str().is_some() && check(value, "text", text) == "in_text" { "in_text" } else { "inferred" },
        k => check(value, k, text),
    }
}

/// The value when it matches the field type, else None (`evidence._clean`: a wrong type is dropped, not coerced).
pub fn clean_value(value: &Value, spec: &Map) -> Value {
    if matches!(value, Value::Null) || value.as_str().is_some_and(|s| typelogic_core::text::strip(s).is_empty()) {
        return Value::Null;
    }
    let kind = spec.get("type").and_then(Value::as_str).unwrap_or("text");
    let name = spec.get("name").and_then(Value::as_str).unwrap_or("");
    if casefmt::check_type(name, value, Some(spec)).is_err() {
        return Value::Null;
    }
    match value {
        Value::Float(f) if kind == "integer" => Value::Int(format!("{f:.0}")),
        Value::Float(f) if !f.is_finite() => Value::Null,
        v => v.clone(),
    }
}

/// JSON schema of the answer (`evidence.json_schema`), for constrained output.
pub fn json_schema(schema: &[Map]) -> Value {
    let mut props = Map::new();
    for f in schema {
        let kind = f.get("type").and_then(Value::as_str).unwrap_or("text");
        let mut base = Map::new();
        match kind {
            "boolean" => base.push_unique("type", Value::str("boolean")),
            "number" => base.push_unique("type", Value::str("number")),
            "integer" => base.push_unique("type", Value::str("integer")),
            "date" => {
                base.push_unique("type", Value::str("string"));
                base.push_unique("format", Value::str("date"));
            }
            "enum" => {
                base.push_unique("type", Value::str("string"));
                base.push_unique("enum", f.get("options").cloned().unwrap_or(Value::Array(Vec::new())));
            }
            _ => base.push_unique("type", Value::str("string")),
        }
        let mut null = Map::new();
        null.push_unique("type", Value::str("null"));
        let name = f.get("name").and_then(Value::as_str).unwrap_or("");
        let description = f.get("description").and_then(Value::as_str).filter(|d| !d.is_empty()).map(str::to_owned).unwrap_or_else(|| name.replace('_', " "));
        let mut p = Map::new();
        p.push_unique("anyOf", Value::Array(vec![Value::Object(base), Value::Object(null)]));
        p.push_unique("description", Value::Str(description));
        props.push_unique(name, Value::Object(p));
    }
    let mut out = Map::new();
    out.push_unique("type", Value::str("object"));
    out.push_unique("properties", Value::Object(props));
    out.push_unique("required", Value::Array(schema.iter().filter_map(|f| f.get("name").cloned()).collect()));
    out.push_unique("additionalProperties", Value::Bool(false));
    Value::Object(out)
}

/// The user message of the extraction (`evidence._ollama_extract`).
pub fn prompt(text: &str, schema: &[Map]) -> String {
    let lines: Vec<String> = schema
        .iter()
        .map(|f| {
            let name = f.get("name").and_then(Value::as_str).unwrap_or("");
            let kind = f.get("type").and_then(Value::as_str).unwrap_or("text");
            let options = f.get("options").and_then(Value::as_array).map(|o| o.iter().filter_map(Value::as_str).collect::<Vec<_>>().join(", "));
            let mut s = format!("- {name} ({kind}");
            if let Some(o) = options.filter(|o| !o.is_empty()) {
                s += &format!(": {o}");
            }
            s.push(')');
            if let Some(d) = f.get("description").and_then(Value::as_str).filter(|d| !d.is_empty()) {
                s += &format!(" - {d}");
            }
            s
        })
        .collect();
    format!("Fields:\n{}\n\nText:\n<<<\n{text}\n>>>", lines.join("\n"))
}

/// Checks of an extraction request: (text, schema) or the message to show.
pub fn validate_request(text: Option<&Value>, schema: Option<&Value>) -> Result<(String, Vec<Map>), String> {
    let text = match text.and_then(Value::as_str) {
        Some(t) if !typelogic_core::text::strip(t).is_empty() => t.to_owned(),
        _ => return Err("Provide the case text to read".into()),
    };
    let n = py_len(&text);
    if n > MAX_TEXT_CHARS {
        return Err(format!("Evidence extraction reads up to {} characters; this text has {}. It is never cut automatically.", casefmt::thousands(MAX_TEXT_CHARS), casefmt::thousands(n)));
    }
    let schema = casefmt::validate_schema(schema)?;
    if schema.is_empty() {
        return Err("Declare the evidence fields to extract (the template defines them)".into());
    }
    let _ = EVIDENCE_TYPES;
    Ok((text, schema))
}

/// The result rows from the model's values (`evidence.extract`, after the model call).
pub fn fields(values: &Map, schema: &[Map], text: &str) -> Value {
    let rows = schema
        .iter()
        .map(|spec| {
            let name = spec.get("name").and_then(Value::as_str).unwrap_or("");
            let kind = spec.get("type").and_then(Value::as_str).unwrap_or("text");
            let value = clean_value(values.get(name).unwrap_or(&Value::Null), spec);
            let check = if matches!(value, Value::Null) { "not_stated" } else { field_check(&value, kind, text) };
            let mut m = Map::new();
            m.push_unique("name", Value::str(name));
            m.push_unique("type", Value::str(kind));
            m.push_unique("value", value);
            m.push_unique("check", Value::str(check));
            m.push_unique("status", Value::str("unverified"));
            Value::Object(m)
        })
        .collect();
    Value::Array(rows)
}

#[cfg(test)]
mod tests {
    use super::*;
    use typelogic_core::json::loads;

    #[test]
    fn checks_match_needle_of_1x() {
        let fixture = loads(include_str!("../tests/evidence_fixture.json"), false).unwrap();
        let rows = fixture.as_array().unwrap();
        assert!(rows.len() > 50);
        for r in rows {
            let r = r.as_object().unwrap();
            let (value, kind, text) = (r.get("value").unwrap(), r.get("kind").unwrap().as_str().unwrap(), r.get("text").unwrap().as_str().unwrap());
            assert_eq!(field_check(value, kind, text), r.get("check").unwrap().as_str().unwrap(), "{value:?} {kind} {text:?}");
        }
    }
}
