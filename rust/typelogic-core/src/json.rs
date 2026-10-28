//! JSON values with Python `json` semantics: ordered objects (last duplicate wins, first position kept),
//! arbitrary-precision integers kept as decimal text, and `json.dumps` output byte-for-byte.
use crate::error::{CoreError, Result};
use crate::pyfloat;
use std::collections::HashMap;

/// Maximum nesting accepted by the parser and by conversions from host languages.
pub const MAX_DEPTH: usize = 400;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    /// Canonical decimal text of a Python `int` (no leading zeros, no `-0`).
    Int(String),
    Float(f64),
    Str(String),
    Array(Vec<Value>),
    Object(Map),
}

/// Insertion-ordered object, like a Python `dict` built by `json.loads`.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Map {
    entries: Vec<(String, Value)>,
}

impl Map {
    pub fn new() -> Self { Self { entries: Vec::new() } }
    pub fn with_capacity(n: usize) -> Self { Self { entries: Vec::with_capacity(n) } }
    pub fn len(&self) -> usize { self.entries.len() }
    pub fn is_empty(&self) -> bool { self.entries.is_empty() }
    pub fn get(&self, key: &str) -> Option<&Value> { self.entries.iter().find(|(k, _)| k == key).map(|(_, v)| v) }
    pub fn contains_key(&self, key: &str) -> bool { self.get(key).is_some() }
    /// `d[key] = value`: replaces in place when present, appends otherwise.
    pub fn insert(&mut self, key: impl Into<String>, value: Value) {
        let key = key.into();
        if let Some(slot) = self.entries.iter_mut().find(|(k, _)| *k == key) {
            slot.1 = value;
        } else {
            self.entries.push((key, value));
        }
    }
    /// Append without a duplicate check; callers guarantee uniqueness.
    pub fn push_unique(&mut self, key: impl Into<String>, value: Value) { self.entries.push((key.into(), value)); }
    pub fn iter(&self) -> impl Iterator<Item = (&String, &Value)> { self.entries.iter().map(|(k, v)| (k, v)) }
    pub fn keys(&self) -> impl Iterator<Item = &String> { self.entries.iter().map(|(k, _)| k) }
    pub fn into_entries(self) -> Vec<(String, Value)> { self.entries }
}

impl Value {
    pub fn str(s: impl Into<String>) -> Self { Value::Str(s.into()) }
    pub fn int(i: i64) -> Self { Value::Int(i.to_string()) }
    pub fn as_str(&self) -> Option<&str> { if let Value::Str(s) = self { Some(s) } else { None } }
    pub fn as_object(&self) -> Option<&Map> { if let Value::Object(m) = self { Some(m) } else { None } }
    pub fn as_array(&self) -> Option<&Vec<Value>> { if let Value::Array(a) = self { Some(a) } else { None } }
    /// Python truthiness.
    pub fn truthy(&self) -> bool {
        match self {
            Value::Null => false,
            Value::Bool(b) => *b,
            Value::Int(s) => s != "0",
            Value::Float(f) => *f != 0.0,
            Value::Str(s) => !s.is_empty(),
            Value::Array(a) => !a.is_empty(),
            Value::Object(m) => !m.is_empty(),
        }
    }
    /// `float(x)` for an int or float (bool excluded by callers). Overflows like CPython.
    pub fn to_f64(&self) -> Result<f64> {
        match self {
            Value::Float(f) => Ok(*f),
            Value::Int(s) => int_to_f64(s),
            Value::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
            _ => Err(CoreError::type_error("must be real number")),
        }
    }
}

/// Convert decimal integer text to `f64` with CPython's `int.__float__` rounding and overflow error.
pub fn int_to_f64(s: &str) -> Result<f64> {
    let v: f64 = s.parse().map_err(|_| CoreError::value("invalid integer"))?;
    if v.is_infinite() {
        return Err(CoreError::overflow("int too large to convert to float"));
    }
    Ok(v)
}

/// Normalize integer text the way `int()` + `repr()` would.
pub fn normalize_int(text: &str) -> String {
    let (neg, digits) = match text.strip_prefix('-') { Some(d) => (true, d), None => (false, text.strip_prefix('+').unwrap_or(text)) };
    let trimmed = digits.trim_start_matches('0');
    if trimmed.is_empty() {
        "0".into()
    } else if neg {
        format!("-{trimmed}")
    } else {
        trimmed.into()
    }
}

// ---------------------------------------------------------------- dumping

/// Options of `json.dumps` used by the project.
#[derive(Debug, Clone, Copy)]
pub struct DumpOptions {
    pub sort_keys: bool,
    /// `separators=(',', ':')` instead of the default `(', ', ': ')`.
    pub compact: bool,
    /// `ensure_ascii=True` escapes every non-ASCII code point.
    pub ensure_ascii: bool,
    /// `allow_nan=False` raises on NaN and infinities.
    pub allow_nan: bool,
}

impl DumpOptions {
    /// `schema.canonical`: sort_keys, ensure_ascii=False, allow_nan=False, default separators.
    pub const CANONICAL: DumpOptions = DumpOptions { sort_keys: true, compact: false, ensure_ascii: false, allow_nan: false };
    /// Record fingerprints and digest checks: compact separators.
    pub const COMPACT_SORTED: DumpOptions = DumpOptions { sort_keys: true, compact: true, ensure_ascii: false, allow_nan: false };
    /// HTTP replies: `json.dumps(value, ensure_ascii=False, allow_nan=False)`.
    pub const REPLY: DumpOptions = DumpOptions { sort_keys: false, compact: false, ensure_ascii: false, allow_nan: false };
}

pub fn dumps(value: &Value, opts: DumpOptions) -> Result<String> {
    let mut out = String::new();
    write_value(&mut out, value, opts, 0)?;
    Ok(out)
}

fn write_value(out: &mut String, value: &Value, opts: DumpOptions, depth: usize) -> Result<()> {
    if depth > MAX_DEPTH {
        return Err(CoreError::unsupported("nesting too deep for the core serializer"));
    }
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Int(s) => out.push_str(s),
        Value::Float(f) => {
            if !f.is_finite() {
                if !opts.allow_nan {
                    return Err(CoreError::value(format!("Out of range float values are not JSON compliant: {}", pyfloat::repr(*f))));
                }
                out.push_str(if f.is_nan() { "NaN" } else if *f > 0.0 { "Infinity" } else { "-Infinity" });
            } else {
                out.push_str(&pyfloat::repr(*f));
            }
        }
        Value::Str(s) => write_string(out, s, opts.ensure_ascii),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(if opts.compact { "," } else { ", " });
                }
                write_value(out, item, opts, depth + 1)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            if opts.sort_keys {
                entries.sort_by(|a, b| a.0.cmp(b.0));
            }
            for (i, (k, v)) in entries.into_iter().enumerate() {
                if i > 0 {
                    out.push_str(if opts.compact { "," } else { ", " });
                }
                write_string(out, k, opts.ensure_ascii);
                out.push_str(if opts.compact { ":" } else { ": " });
                write_value(out, v, opts, depth + 1)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

pub fn write_string(out: &mut String, s: &str, ensure_ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if ensure_ascii && (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", unit));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

// ---------------------------------------------------------------- parsing

/// Parse JSON text like `json.loads`. When `reject_constants` is true, `NaN`/`Infinity`/`-Infinity`
/// are refused (the project's `parse_constant` hook).
pub fn loads(text: &str, reject_constants: bool) -> Result<Value> {
    let mut p = Parser { s: text.as_bytes(), i: 0, reject_constants, text };
    p.ws();
    let v = p.value(0)?;
    p.ws();
    if p.i != p.s.len() {
        return Err(p.err("Extra data"));
    }
    Ok(v)
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
    reject_constants: bool,
    text: &'a str,
}

impl<'a> Parser<'a> {
    fn err(&self, msg: &str) -> CoreError {
        // Same position convention as Python: line, column (1-based) and character offset.
        let before = &self.text[..self.i.min(self.text.len())];
        let line = before.matches('\n').count() + 1;
        let col = before.rsplit('\n').next().map(|l| l.chars().count()).unwrap_or(0) + 1;
        CoreError::value(format!("{msg}: line {line} column {col} (char {})", before.chars().count()))
    }
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }
    fn starts(&self, lit: &str) -> bool { self.s[self.i..].starts_with(lit.as_bytes()) }
    fn constant(&mut self, lit: &str, value: f64) -> Result<Value> {
        if self.reject_constants {
            return Err(CoreError::value("JSON must contain finite values"));
        }
        self.i += lit.len();
        Ok(Value::Float(value))
    }
    fn value(&mut self, depth: usize) -> Result<Value> {
        if depth > MAX_DEPTH {
            return Err(CoreError::value("JSON nesting too deep"));
        }
        if self.i >= self.s.len() {
            return Err(self.err("Expecting value"));
        }
        match self.s[self.i] {
            b'{' => self.object(depth),
            b'[' => self.array(depth),
            b'"' => Ok(Value::Str(self.string()?)),
            b'n' if self.starts("null") => { self.i += 4; Ok(Value::Null) }
            b't' if self.starts("true") => { self.i += 4; Ok(Value::Bool(true)) }
            b'f' if self.starts("false") => { self.i += 5; Ok(Value::Bool(false)) }
            b'N' if self.starts("NaN") => self.constant("NaN", f64::NAN),
            b'I' if self.starts("Infinity") => self.constant("Infinity", f64::INFINITY),
            b'-' if self.starts("-Infinity") => self.constant("-Infinity", f64::NEG_INFINITY),
            b'-' | b'0'..=b'9' => self.number(),
            _ => Err(self.err("Expecting value")),
        }
    }
    fn number(&mut self) -> Result<Value> {
        let start = self.i;
        if self.s[self.i] == b'-' {
            self.i += 1;
        }
        let digits = |p: &mut Self| { let b = p.i; while p.i < p.s.len() && p.s[p.i].is_ascii_digit() { p.i += 1; } p.i - b };
        if self.i < self.s.len() && self.s[self.i] == b'0' {
            self.i += 1;
        } else if digits(self) == 0 {
            self.i = start;
            return Err(self.err("Expecting value"));
        }
        let int_end = self.i;
        let mut is_float = false;
        if self.i + 1 < self.s.len() && self.s[self.i] == b'.' && self.s[self.i + 1].is_ascii_digit() {
            self.i += 1;
            digits(self);
            is_float = true;
        }
        if self.i < self.s.len() && matches!(self.s[self.i], b'e' | b'E') {
            let save = self.i;
            self.i += 1;
            if self.i < self.s.len() && matches!(self.s[self.i], b'+' | b'-') {
                self.i += 1;
            }
            if digits(self) == 0 {
                self.i = save;
            } else {
                is_float = true;
            }
        }
        let text = &self.text[start..self.i];
        if is_float {
            Ok(Value::Float(text.parse::<f64>().map_err(|_| self.err("Invalid number"))?))
        } else {
            Ok(Value::Int(normalize_int(&self.text[start..int_end])))
        }
    }
    fn string(&mut self) -> Result<String> {
        self.i += 1; // opening quote
        let mut out = String::new();
        loop {
            let run_start = self.i;
            while self.i < self.s.len() && self.s[self.i] != b'"' && self.s[self.i] != b'\\' && self.s[self.i] >= 0x20 {
                self.i += 1;
            }
            out.push_str(&self.text[run_start..self.i]);
            if self.i >= self.s.len() {
                return Err(self.err("Unterminated string starting at"));
            }
            match self.s[self.i] {
                b'"' => { self.i += 1; return Ok(out); }
                b'\\' => {
                    self.i += 1;
                    if self.i >= self.s.len() {
                        return Err(self.err("Unterminated string starting at"));
                    }
                    let c = self.s[self.i];
                    self.i += 1;
                    match c {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let hi = self.hex4()?;
                            if (0xD800..0xDC00).contains(&hi) && self.starts("\\u") {
                                let save = self.i;
                                self.i += 2;
                                let lo = self.hex4()?;
                                if (0xDC00..0xE000).contains(&lo) {
                                    let cp = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00);
                                    out.push(char::from_u32(cp).expect("valid pair"));
                                    continue;
                                }
                                self.i = save;
                            }
                            match char::from_u32(hi) {
                                Some(ch) => out.push(ch),
                                None => return Err(CoreError::unsupported("lone UTF-16 surrogate in JSON string")),
                            }
                        }
                        _ => { self.i -= 2; return Err(self.err("Invalid \\escape")); }
                    }
                }
                _ => return Err(self.err("Invalid control character at")),
            }
        }
    }
    fn hex4(&mut self) -> Result<u32> {
        if self.i + 4 > self.s.len() {
            return Err(self.err("Invalid \\uXXXX escape"));
        }
        let h = std::str::from_utf8(&self.s[self.i..self.i + 4]).ok().and_then(|t| u32::from_str_radix(t, 16).ok());
        match h {
            Some(v) if self.s[self.i..self.i + 4].iter().all(|b| b.is_ascii_hexdigit()) => { self.i += 4; Ok(v) }
            _ => Err(self.err("Invalid \\uXXXX escape")),
        }
    }
    fn array(&mut self, depth: usize) -> Result<Value> {
        self.i += 1;
        let mut items = Vec::new();
        self.ws();
        if self.i < self.s.len() && self.s[self.i] == b']' {
            self.i += 1;
            return Ok(Value::Array(items));
        }
        loop {
            self.ws();
            items.push(self.value(depth + 1)?);
            self.ws();
            match self.s.get(self.i) {
                Some(b',') => self.i += 1,
                Some(b']') => { self.i += 1; return Ok(Value::Array(items)); }
                _ => return Err(self.err("Expecting ',' delimiter")),
            }
        }
    }
    fn object(&mut self, depth: usize) -> Result<Value> {
        self.i += 1;
        let mut map = Map::new();
        let mut index: HashMap<String, usize> = HashMap::new();
        self.ws();
        if self.i < self.s.len() && self.s[self.i] == b'}' {
            self.i += 1;
            return Ok(Value::Object(map));
        }
        loop {
            self.ws();
            if self.s.get(self.i) != Some(&b'"') {
                return Err(self.err("Expecting property name enclosed in double quotes"));
            }
            let key = self.string()?;
            self.ws();
            if self.s.get(self.i) != Some(&b':') {
                return Err(self.err("Expecting ':' delimiter"));
            }
            self.i += 1;
            self.ws();
            let v = self.value(depth + 1)?;
            match index.get(&key) {
                Some(&pos) => map.entries[pos].1 = v,
                None => { index.insert(key.clone(), map.entries.len()); map.entries.push((key, v)); }
            }
            self.ws();
            match self.s.get(self.i) {
                Some(b',') => self.i += 1,
                Some(b'}') => { self.i += 1; return Ok(Value::Object(map)); }
                _ => return Err(self.err("Expecting ',' delimiter")),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_matches_python_dumps() {
        let v = loads(r#"{"b":[1,2.5,-0,1e400],"a":"xé\n","a":3}"#, false).unwrap();
        assert!(dumps(&v, DumpOptions::CANONICAL).is_err()); // inf refused
        let v = loads(r#"{"b":[1,2.5,-0,true,null],"a":"xé\n\u0001","a":3}"#, false).unwrap();
        assert_eq!(dumps(&v, DumpOptions::CANONICAL).unwrap(), r#"{"a": 3, "b": [1, 2.5, 0, true, null]}"#);
        assert_eq!(dumps(&v, DumpOptions::REPLY).unwrap(), r#"{"b": [1, 2.5, 0, true, null], "a": 3}"#);
        let s = loads(r#""xé\n\u0001😀""#, false).unwrap();
        assert_eq!(dumps(&s, DumpOptions::COMPACT_SORTED).unwrap(), "\"xé\\n\\u0001😀\"");
    }

    #[test]
    fn constants_and_surrogates() {
        assert!(loads("NaN", true).is_err());
        assert!(matches!(loads("NaN", false).unwrap(), Value::Float(f) if f.is_nan()));
        assert_eq!(loads(r#""\ud800""#, false).unwrap_err().kind, crate::error::ErrorKind::Unsupported);
        assert!(loads("[1,]", false).is_err() && loads("01", false).is_err() && loads("\"\u{1}\"", false).is_err());
    }
}
