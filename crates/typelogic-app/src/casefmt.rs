//! Port of `typelogic/casefmt.py`: the Case / Evidence / Decision request (format v2), titles and tags.
//!
//! A case request (`{"case", "evidence", "decisions", ...}`) is converted into the established contract
//! (`state` + `questions`), so the engines, the integrity checks and the exports work unchanged. Messages and
//! the rendered state are byte-identical to 1.x (checked against the Python output in tests/casefmt_fixture.json).
use typelogic_core::json::{dumps, Map, Value, DumpOptions};
use typelogic_core::pyfloat;
use typelogic_core::text::{is_space, py_len, strip};

pub const EVIDENCE_TYPES: [&str; 6] = ["boolean", "number", "integer", "date", "text", "enum"];
const SOURCES: [&str; 3] = ["user", "extracted", "template"];
const MAX_EVIDENCE: usize = 64;
const MAX_ATTACHMENTS: usize = 10;
const MAX_ATTACHMENT_CHARS: usize = 40000;
const MAX_TEXT: usize = 1000;
const PASSTHROUGH: [&str; 3] = ["space", "threshold", "permutation_check"];
pub const MAX_TAGS: usize = 8;
pub const MAX_TAG_CHARS: usize = 32;
pub const MAX_TITLE_CHARS: usize = 120;
/// Suggested tags (the web app offers them while typing); any other tag can be typed too.
pub const TAGS: [&str; 20] = [
    "billing", "refund", "fraud", "security", "compliance", "legal", "support", "technical", "sales", "purchasing", "logistics",
    "inventory", "hr", "finance", "it-support", "urgent", "low-priority", "escalation", "customer", "internal",
];

type R<T> = Result<T, String>;

/// `^[a-z][a-z0-9_]{0,63}$`
pub fn is_name(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty() && b.len() <= 64 && b[0].is_ascii_lowercase() && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_')
}

/// Python `f"{n:,}"`.
pub fn thousands(n: usize) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// `' '.join(s.split())`
pub fn collapse(s: &str) -> String { s.split(is_space).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ") }

/// Python `repr(s)` of a string (quotes and escapes as Python chooses them).
pub fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn text_field(value: Option<&Value>, label: &str, limit: usize, required: bool) -> R<String> {
    match value {
        None | Some(Value::Null) if !required => return Ok(String::new()),
        _ => {}
    }
    let s = value.and_then(Value::as_str);
    if required && s.is_none_or(|s| strip(s).is_empty()) {
        return Err(format!("{label}: write it (it is required)"));
    }
    match s {
        Some(s) if py_len(s) <= limit => Ok(strip(s).to_owned()),
        _ => Err(format!("{label}: text up to {} characters", thousands(limit))),
    }
}

fn is_int(v: &Value) -> bool { matches!(v, Value::Int(_)) }
fn float_of(v: &Value) -> Option<f64> {
    match v {
        Value::Float(f) => Some(*f),
        Value::Int(s) => typelogic_core::json::int_to_f64(s).ok(),
        _ => None,
    }
}

fn scalar(value: Option<&Value>, name: &str) -> R<Value> {
    let v = value.cloned().unwrap_or(Value::Null);
    match &v {
        Value::Null | Value::Bool(_) => Ok(v),
        Value::Str(s) if py_len(s) <= MAX_TEXT => Ok(v),
        Value::Int(_) => Ok(v),
        Value::Float(f) if f.is_finite() => Ok(v),
        _ => Err(format!("Evidence {name}: value must be text (up to {MAX_TEXT} characters), a finite number, true/false or null")),
    }
}

/// Checked evidence schema (`validate_schema`).
pub fn validate_schema(schema: Option<&Value>) -> R<Vec<Map>> {
    let items = match schema {
        None | Some(Value::Null) => return Ok(Vec::new()),
        Some(Value::Array(a)) if a.len() <= MAX_EVIDENCE => a,
        _ => return Err(format!("evidence_schema: a list of up to {MAX_EVIDENCE} fields")),
    };
    let mut out = Vec::new();
    let mut seen = Vec::<String>::new();
    for f in items {
        let m = f.as_object();
        let name = m.and_then(|m| m.get("name")).and_then(Value::as_str);
        let Some((m, name)) = m.zip(name).filter(|(_, n)| is_name(n) && !seen.iter().any(|s| s == n)) else {
            return Err("evidence_schema: each field needs a unique name (lowercase letters, digits, _)".into());
        };
        let kind = match m.get("type") {
            None => "text",
            Some(Value::Str(k)) if EVIDENCE_TYPES.contains(&k.as_str()) => k.as_str(),
            _ => return Err(format!("evidence_schema {name}: type must be one of {}", EVIDENCE_TYPES.join(", "))),
        };
        let mut item = Map::new();
        item.push_unique("name", Value::str(name));
        item.push_unique("type", Value::str(kind));
        item.push_unique("description", Value::Str(text_field(m.get("description"), &format!("{name} description"), 300, false)?));
        if kind == "enum" {
            let ok = match m.get("options") {
                Some(Value::Array(o)) if (2..=32).contains(&o.len()) => {
                    let strs: Vec<Option<&str>> = o.iter().map(Value::as_str).collect();
                    let unique = (0..o.len()).all(|i| (0..i).all(|j| o[i] != o[j]));
                    unique && strs.iter().all(|s| s.is_some_and(|s| !strip(s).is_empty() && py_len(s) <= 100))
                }
                _ => false,
            };
            if !ok {
                return Err(format!("evidence_schema {name}: an enum needs 2-32 unique options"));
            }
            item.push_unique("options", m.get("options").cloned().unwrap_or(Value::Null));
        }
        if m.get("unit").is_some_and(Value::truthy) {
            item.push_unique("unit", Value::Str(text_field(m.get("unit"), &format!("{name} unit"), 30, false)?));
        }
        if m.get("required").is_some_and(Value::truthy) {
            item.push_unique("required", Value::Bool(true));
        }
        seen.push(name.to_owned());
        out.push(item);
    }
    Ok(out)
}

/// `date.fromisoformat(value)` for the YYYY-MM-DD form, with a real calendar date.
pub fn iso_date(value: &str) -> Option<(i32, u32, u32)> {
    let b = value.as_bytes();
    if b.len() != 10 || b[4] != b'-' || b[7] != b'-' || !b.iter().enumerate().all(|(i, c)| i == 4 || i == 7 || c.is_ascii_digit()) {
        return None;
    }
    let y: i32 = value[0..4].parse().ok()?;
    let m: u32 = value[5..7].parse().ok()?;
    let d: u32 = value[8..10].parse().ok()?;
    let leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
    let days = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    (y >= 1 && (1..=12).contains(&m) && d >= 1 && d <= days[(m - 1) as usize]).then_some((y, m, d))
}

/// `_check_type(name, value, spec)`.
pub fn check_type(name: &str, value: &Value, spec: Option<&Map>) -> R<()> {
    let Some(spec) = spec else { return Ok(()) };
    if matches!(value, Value::Null) {
        return Ok(());
    }
    let kind = spec.get("type").and_then(Value::as_str).unwrap_or("text");
    let options: Vec<&str> = spec.get("options").and_then(Value::as_array).map(|o| o.iter().filter_map(Value::as_str).collect()).unwrap_or_default();
    let ok = match kind {
        "boolean" => matches!(value, Value::Bool(_)),
        "number" => matches!(value, Value::Int(_) | Value::Float(_)),
        "integer" => is_int(value) || matches!(value, Value::Float(f) if f.fract() == 0.0 && f.is_finite()),
        "text" => matches!(value, Value::Str(_)),
        "enum" => value.as_str().is_some_and(|s| options.contains(&s)),
        _ => value.as_str().is_some_and(|s| iso_date(s).is_some()),
    };
    if ok {
        return Ok(());
    }
    Err(format!("Evidence {name}: expected {kind}") + &if kind == "enum" { format!(" ({})", options.join(", ")) } else { String::new() })
}

/// Python `int(f)` for an integral float, as a JSON integer.
fn int_of_float(f: f64) -> Value { Value::Int(format!("{f:.0}")) }

/// `validate_evidence(evidence, schema)`: `{name: {value, source, status[, check]}}`.
pub fn validate_evidence(evidence: Option<&Value>, schema: &[Map]) -> R<Map> {
    let empty = Map::new();
    let entries = match evidence {
        None | Some(Value::Null) => &empty,
        Some(Value::Object(m)) if m.len() <= MAX_EVIDENCE => m,
        _ => return Err(format!("evidence: an object with up to {MAX_EVIDENCE} fields")),
    };
    let spec_of = |n: &str| schema.iter().find(|s| s.get("name").and_then(Value::as_str) == Some(n));
    let mut out = Map::new();
    for (name, entry) in entries.iter() {
        if !is_name(name) {
            return Err(format!("Evidence name {}: lowercase letters, digits and _, starting with a letter", py_repr(name)));
        }
        let (mut value, source, status, check) = match entry {
            Value::Object(e) => {
                let value = scalar(e.get("value"), name)?;
                let source = match e.get("source") {
                    None => "user".to_owned(),
                    Some(Value::Str(s)) if SOURCES.contains(&s.as_str()) => s.clone(),
                    _ => return Err(format!("Evidence {name}: source must be one of {}", SOURCES.join(", "))),
                };
                let status = match e.get("status") {
                    v if v.is_none_or(|v| !v.truthy()) => if source == "extracted" { "unverified" } else { "confirmed" }.to_owned(),
                    Some(Value::Str(s)) if s == "confirmed" || s == "unverified" => s.clone(),
                    _ => return Err(format!("Evidence {name}: status must be confirmed or unverified")),
                };
                let check = match e.get("check") {
                    None | Some(Value::Null) => None,
                    Some(Value::Str(c)) if ["in_text", "not_in_text", "inferred", "not_stated"].contains(&c.as_str()) => Some(c.clone()),
                    _ => return Err(format!("Evidence {name}: invalid check")),
                };
                (value, source, status, check)
            }
            other => (scalar(Some(other), name)?, "user".to_owned(), "confirmed".to_owned(), None),
        };
        let spec = spec_of(name);
        if spec.and_then(|s| s.get("type")).and_then(Value::as_str) == Some("integer") {
            if let Value::Float(f) = value {
                if f.fract() == 0.0 {
                    value = int_of_float(f);
                }
            }
        }
        check_type(name, &value, spec)?;
        let mut e = Map::new();
        e.push_unique("value", value);
        e.push_unique("source", Value::Str(source));
        e.push_unique("status", Value::Str(status));
        if let Some(c) = check.filter(|c| !c.is_empty()) {
            e.push_unique("check", Value::Str(c));
        }
        out.insert(name.clone(), Value::Object(e));
    }
    Ok(out)
}

/// `validate_tags(tags)`: unique lowercase labels with collapsed spaces.
pub fn validate_tags(tags: Option<&Value>) -> R<Vec<String>> {
    let items = match tags {
        None | Some(Value::Null) => return Ok(Vec::new()),
        Some(Value::Array(a)) if a.len() <= MAX_TAGS => a,
        _ => return Err(format!("tags: a list of up to {MAX_TAGS} tags")),
    };
    let mut out: Vec<String> = Vec::new();
    for tag in items {
        let Some(tag) = tag.as_str() else { return Err("tags: each tag is a text".into()) };
        let label = collapse(tag).to_lowercase();
        let n = py_len(&label);
        if !(1..=MAX_TAG_CHARS).contains(&n) || label.chars().any(|c| (c as u32) < 32 || "<>\"\\".contains(c)) {
            return Err(format!("Tag {}: 1-{MAX_TAG_CHARS} characters, without < > \" or \\", py_repr(tag)));
        }
        if !out.contains(&label) {
            out.push(label);
        }
    }
    Ok(out)
}

/// `validate_title(title)`: the typed title with collapsed spaces, or None.
pub fn validate_title(title: Option<&Value>) -> R<Option<String>> {
    match title {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Str(t)) => {
            let t = collapse(t);
            if py_len(&t) > MAX_TITLE_CHARS {
                return Err(format!("title: up to {MAX_TITLE_CHARS} characters"));
            }
            Ok((!t.is_empty()).then_some(t))
        }
        Some(_) => Err("title: a text".into()),
    }
}

/// Python `str(key)` of a JSON value used as an alternative name.
fn key_text(v: &Value) -> String {
    match v {
        Value::Str(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Null => "None".into(),
        Value::Int(s) => s.clone(),
        Value::Float(f) => pyfloat::repr(*f),
        other => dumps(other, DumpOptions::REPLY).unwrap_or_default(),
    }
}

/// `-?\d+(\.\d+)?` (ASCII digits; other scripts' digits are parsed by Python too, through their decimal value).
fn numeric_key(k: &str) -> Option<f64> {
    let digits: String = k
        .chars()
        .map(|c| typelogic_core::text::decimal_value(c).map(|d| char::from(b'0' + d as u8)).unwrap_or(c))
        .collect();
    let body = digits.strip_prefix('-').unwrap_or(&digits);
    let (int, frac) = match body.split_once('.') {
        Some((i, f)) => (i, Some(f)),
        None => (body, None),
    };
    let ok = !int.is_empty() && int.bytes().all(|c| c.is_ascii_digit()) && frac.is_none_or(|f| !f.is_empty() && f.bytes().all(|c| c.is_ascii_digit()));
    if ok { digits.parse().ok() } else { None }
}

fn decisions(payload: &Map) -> R<Map> {
    let items: Vec<Value> = match (payload.get("decisions"), payload.get("decision")) {
        (None | Some(Value::Null), Some(d)) if !matches!(d, Value::Null) => vec![d.clone()],
        (Some(Value::Array(a)), _) if (1..=32).contains(&a.len()) => a.clone(),
        _ => return Err("Provide a decision or a list of 1-32 decisions".into()),
    };
    let mut questions = Map::new();
    for (i, d) in items.iter().enumerate() {
        let i = i + 1;
        let Some(d) = d.as_object() else { return Err("Each decision must be an object".into()) };
        let name = match d.get("name") {
            Some(v) if v.truthy() => v.as_str().map(str::to_owned),
            _ => Some(format!("decision_{i}")),
        };
        let Some(name) = name.filter(|n| is_name(n) && !questions.contains_key(n)) else {
            return Err(format!("Decision {i}: a name, when given, must be unique (lowercase letters, digits and _)"));
        };
        let label = format!("Decision {i}");
        let kind = match d.get("output").and_then(Value::as_str) {
            Some("boolean" | "noul") => "noul",
            Some("choice") => "choice",
            Some("score") => "score",
            _ => return Err(format!("{label}: output must be boolean (Yes / no), choice or score")),
        };
        let instructions = text_field(d.get("instructions"), &format!("{label} question"), 6000, true)?;
        let mut q = Map::new();
        q.push_unique("type", Value::str(kind));
        q.push_unique("instructions", Value::Str(instructions));
        match kind {
            "choice" => {
                let alternatives = d.get("alternatives").or_else(|| d.get("criteria"));
                let alternatives = match alternatives {
                    Some(Value::Array(a)) => {
                        let mut m = Map::new();
                        for v in a {
                            m.insert(key_text(v), Value::str(""));
                        }
                        Some(m)
                    }
                    Some(Value::Object(m)) => Some(m.clone()),
                    _ => None,
                };
                let Some(alternatives) = alternatives.filter(|m| m.len() >= 2) else {
                    return Err(format!("{label}: a choice needs at least two options (returned as: choose when)"));
                };
                q.push_unique("criteria", Value::Object(alternatives));
                if let Some(a) = d.get("abstain_option").filter(|a| !matches!(a, Value::Null)) {
                    q.push_unique("abstain_option", a.clone());
                }
            }
            "noul" => {
                let criteria = match d.get("criteria") {
                    None => Map::new(),
                    Some(v) if !v.truthy() => Map::new(),
                    Some(Value::Object(m)) if m.keys().all(|k| k == "true" || k == "false") => m.clone(),
                    _ => return Err(format!("{label}: Yes / no criteria may only describe \"true\" and \"false\"")),
                };
                let mut kept = Map::new();
                for (k, v) in criteria.iter() {
                    if let Some(s) = v.as_str().map(strip).filter(|s| !s.is_empty()) {
                        kept.push_unique(k.clone(), Value::str(s));
                    }
                }
                if !kept.is_empty() {
                    q.push_unique("criteria", Value::Object(kept));
                }
            }
            _ => {
                let levels = d.get("levels").or_else(|| d.get("criteria"));
                let levels = match levels {
                    Some(Value::Object(m)) => {
                        let mut keys: Vec<&String> = m.keys().collect();
                        let numeric: Option<Vec<f64>> = keys.iter().map(|k| numeric_key(k)).collect();
                        if let Some(values) = numeric {
                            let mut pairs: Vec<(f64, &String)> = values.into_iter().zip(keys).collect();
                            pairs.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal)); // stable, like sorted()
                            keys = pairs.into_iter().map(|(_, k)| k).collect();
                        }
                        Value::Array(keys.into_iter().map(|k| m.get(k).cloned().unwrap_or(Value::Null)).collect())
                    }
                    Some(Value::Array(a)) => Value::Array(a.clone()),
                    _ => return Err(format!("{label}: a score needs levels, from lowest to highest")),
                };
                q.push_unique("criteria", levels);
            }
        }
        questions.push_unique(name, Value::Object(q));
    }
    Ok(questions)
}

fn attachments(items: Option<&Value>) -> R<Vec<Map>> {
    let items = match items {
        None | Some(Value::Null) => return Ok(Vec::new()),
        Some(Value::Array(a)) if a.len() <= MAX_ATTACHMENTS => a,
        _ => return Err(format!("attachments: a list of up to {MAX_ATTACHMENTS} items")),
    };
    let mut out = Vec::new();
    for a in items {
        let Some(a) = a.as_object() else { return Err("Each attachment must be an object".into()) };
        let kind = match a.get("type") {
            None => "document",
            Some(Value::Str(k)) if k == "image" => return Err("Image attachments are not supported: the local engines read text only".into()),
            Some(Value::Str(k)) if k == "document" || k == "text" => k.as_str(),
            _ => return Err("Attachment type must be document or text".into()),
        };
        let content = match a.get("content") {
            Some(Value::Str(c)) if !strip(c).is_empty() => c.clone(),
            _ => return Err("Each attachment needs its text content".into()),
        };
        if py_len(&content) > MAX_ATTACHMENT_CHARS {
            return Err(format!("Attachment text is limited to {} characters (it is never cut automatically)", thousands(MAX_ATTACHMENT_CHARS)));
        }
        let title = text_field(a.get("title"), "Attachment title", 300, false)?;
        let mut m = Map::new();
        m.push_unique("type", Value::str(kind));
        m.push_unique("title", Value::Str(if title.is_empty() { "Attachment".into() } else { title }));
        m.push_unique("content", Value::Str(content));
        out.push(m);
    }
    Ok(out)
}

/// The case as one text: a string, or the earlier `{"summary", "details"}` object.
pub fn case_text(case: Option<&Value>) -> R<String> {
    match case {
        Some(Value::Str(s)) if !strip(s).is_empty() => return text_field(case, "case", 100_000, false),
        Some(Value::Object(m)) => {
            let summary = text_field(m.get("summary"), "case.summary", 1000, false)?;
            let details = text_field(m.get("details").or_else(|| m.get("text")), "case.details", 100_000, false)?;
            if !summary.is_empty() && !details.is_empty() && !details.contains(&summary) {
                return Ok(format!("{summary}\n\n{details}"));
            }
            if !summary.is_empty() || !details.is_empty() {
                return Ok(if details.is_empty() { summary } else { details });
            }
        }
        _ => {}
    }
    Err("case: describe the case (a text)".into())
}

/// Python `str(value)` of an evidence value.
pub fn value_text(v: &Value) -> String {
    match v {
        Value::Bool(true) => "true".into(),
        Value::Bool(false) => "false".into(),
        Value::Str(s) => s.clone(),
        Value::Int(s) => s.clone(),
        Value::Float(f) => pyfloat::repr(*f),
        Value::Null => "None".into(),
        other => dumps(other, DumpOptions::REPLY).unwrap_or_default(),
    }
}

/// The state the engines read (`render_state`).
pub fn render_state(text: &str, values: &Map, attachments: &[Map], style: &str) -> Value {
    if style == "json" {
        let mut state = Map::new();
        state.push_unique("case", Value::str(text));
        state.push_unique("evidence", Value::Object(values.clone()));
        if !attachments.is_empty() {
            let rows = attachments
                .iter()
                .map(|a| {
                    let mut m = Map::new();
                    m.push_unique("title", a.get("title").cloned().unwrap_or(Value::Null));
                    m.push_unique("content", a.get("content").cloned().unwrap_or(Value::Null));
                    Value::Object(m)
                })
                .collect();
            state.push_unique("attachments", Value::Array(rows));
        }
        return Value::Object(state);
    }
    let mut parts = vec![strip(text).to_owned()];
    if !values.is_empty() {
        let mut keys: Vec<&String> = values.keys().collect();
        keys.sort();
        let lines: Vec<String> = keys.iter().map(|k| format!("- {k}: {}", value_text(values.get(k).unwrap_or(&Value::Null)))).collect();
        parts.push(format!("Evidence:\n{}", lines.join("\n")));
    }
    for a in attachments {
        let title = a.get("title").and_then(Value::as_str).unwrap_or("");
        let content = a.get("content").and_then(Value::as_str).unwrap_or("");
        parts.push(format!("Attachment: {title}\n{}", strip(content)));
    }
    Value::Str(parts.join("\n\n"))
}

pub fn is_v2(payload: &Value) -> bool { payload.as_object().is_some_and(|m| ["case", "decision", "decisions"].iter().any(|k| m.contains_key(k))) }

#[derive(Debug, Clone, PartialEq)]
pub struct CaseInfo {
    pub missing_required: Vec<String>,
    pub unconfirmed: Vec<String>,
    pub case: String,
}

/// `normalize(payload)`: (internal request with state + questions, case information).
pub fn normalize(payload: &Map) -> R<(Map, CaseInfo)> {
    let text = case_text(payload.get("case"))?;
    let schema = validate_schema(payload.get("evidence_schema"))?;
    let evidence = validate_evidence(payload.get("evidence"), &schema)?;
    let attachments = attachments(payload.get("attachments"))?;
    let questions = decisions(payload)?;
    let mut values = Map::new();
    for (k, v) in evidence.iter() {
        let value = v.as_object().and_then(|e| e.get("value")).cloned().unwrap_or(Value::Null);
        if !matches!(value, Value::Null) {
            values.push_unique(k.clone(), value);
        }
    }
    let engine_input = match payload.get("engine_input") {
        None => "text",
        Some(Value::Str(s)) if s == "text" || s == "json" => s.as_str(),
        _ => return Err("engine_input must be text or json".into()),
    };
    let state = render_state(&text, &values, &attachments, engine_input);
    let mut internal = Map::new();
    for k in PASSTHROUGH {
        if let Some(v) = payload.get(k) {
            internal.push_unique(k, v.clone());
        }
    }
    internal.insert("state", state);
    internal.insert("questions", Value::Object(questions.clone()));
    let template = match payload.get("template") {
        None | Some(Value::Null) => None,
        Some(Value::Str(t)) if py_len(t) <= 100 => Some(t.clone()),
        _ => return Err("template: the id of a decision template".into()),
    };
    let mut case_request = Map::new();
    case_request.push_unique("format", Value::str("typelogic-case-v2"));
    case_request.push_unique("engine_input", Value::str(engine_input));
    case_request.push_unique("case", Value::str(&text));
    case_request.push_unique("evidence", Value::Object(evidence.clone()));
    case_request.push_unique("decision_order", Value::Array(questions.keys().map(Value::str).collect()));
    case_request.push_unique("evidence_schema", Value::Array(schema.iter().cloned().map(Value::Object).collect()));
    case_request.push_unique("attachments", Value::Array(attachments.iter().cloned().map(Value::Object).collect()));
    if let Some(t) = template.filter(|t| !t.is_empty()) {
        case_request.push_unique("template", Value::Str(t));
    }
    internal.insert("case_request", Value::Object(case_request));
    let value_of = |n: &str| evidence.get(n).and_then(Value::as_object).and_then(|e| e.get("value")).cloned().unwrap_or(Value::Null);
    let missing = schema
        .iter()
        .filter(|f| f.get("required").is_some_and(Value::truthy))
        .filter_map(|f| f.get("name").and_then(Value::as_str))
        .filter(|n| matches!(value_of(n), Value::Null))
        .map(str::to_owned)
        .collect();
    let unconfirmed = evidence
        .iter()
        .filter(|(_, v)| v.as_object().and_then(|e| e.get("status")).and_then(Value::as_str) == Some("unverified"))
        .map(|(k, _)| k.clone())
        .collect();
    Ok((internal, CaseInfo { missing_required: missing, unconfirmed, case: text }))
}

/// `apply_to_response(response, info)`: flag answers resting on unconfirmed or missing evidence.
pub fn apply_to_response(response: &mut Map, info: &CaseInfo) {
    let mut reasons = Vec::new();
    if !info.unconfirmed.is_empty() {
        reasons.push("unconfirmed_evidence");
    }
    if !info.missing_required.is_empty() {
        reasons.push("missing_required_evidence");
    }
    if let Some(Value::Object(answers)) = response.get("answers").cloned() {
        let mut out = Map::new();
        for (k, a) in answers.into_entries() {
            let Value::Object(mut a) = a else {
                out.push_unique(k, a);
                continue;
            };
            let mut r = a.get("review_reasons").and_then(Value::as_array).cloned().unwrap_or_default();
            for reason in &reasons {
                if !r.iter().any(|x| x.as_str() == Some(reason)) {
                    r.push(Value::str(*reason));
                }
            }
            a.insert("review_reasons", Value::Array(r));
            if !reasons.is_empty() {
                a.insert("recommendation", Value::str("review"));
            }
            out.push_unique(k, Value::Object(a));
        }
        response.insert("answers", Value::Object(out));
    }
    let mut review = Map::new();
    review.push_unique("unconfirmed", Value::Array(info.unconfirmed.iter().map(Value::str).collect()));
    review.push_unique("missing_required", Value::Array(info.missing_required.iter().map(Value::str).collect()));
    response.insert("evidence_review", Value::Object(review));
}

/// Number for a JSON value when it is a number (bools are not).
pub fn number(v: &Value) -> Option<f64> { float_of(v) }

#[cfg(test)]
mod tests {
    use super::*;
    use typelogic_core::json::loads;

    /// Python 1.x outputs (spike/work/mkcase.py): every template as a request, edge cases and their errors.
    #[test]
    fn matches_the_1x_case_format() {
        let fixture = loads(include_str!("../tests/casefmt_fixture.json"), false).unwrap();
        let cases = fixture.as_object().unwrap().get("normalize").unwrap().as_array().unwrap();
        assert!(cases.len() > 20);
        for c in cases {
            let c = c.as_object().unwrap();
            let payload = c.get("payload").unwrap().as_object().unwrap();
            let got = normalize(payload);
            match (c.get("error"), got) {
                (Some(Value::Str(e)), Err(g)) => assert_eq!(&g, e),
                (Some(Value::Str(e)), Ok(_)) => panic!("expected error {e}"),
                (_, Err(g)) => panic!("unexpected error {g} for {}", dumps(c.get("payload").unwrap(), DumpOptions::REPLY).unwrap()),
                (_, Ok((internal, info))) => {
                    let want = dumps(c.get("internal").unwrap(), DumpOptions::COMPACT_SORTED).unwrap();
                    assert_eq!(dumps(&Value::Object(internal), DumpOptions::COMPACT_SORTED).unwrap(), want);
                    let i = c.get("info").unwrap().as_object().unwrap();
                    let list = |k: &str| i.get(k).unwrap().as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_owned()).collect::<Vec<_>>();
                    assert_eq!(info.missing_required, list("missing_required"));
                    assert_eq!(info.unconfirmed, list("unconfirmed"));
                }
            }
        }
        for c in fixture.as_object().unwrap().get("tags").unwrap().as_array().unwrap() {
            let c = c.as_object().unwrap();
            let got = validate_tags(c.get("tags"));
            match c.get("error") {
                Some(Value::Str(e)) => assert_eq!(got.unwrap_err(), *e),
                _ => assert_eq!(Value::Array(got.unwrap().into_iter().map(Value::Str).collect()), *c.get("out").unwrap()),
            }
        }
        for c in fixture.as_object().unwrap().get("titles").unwrap().as_array().unwrap() {
            let c = c.as_object().unwrap();
            let got = validate_title(c.get("title"));
            match c.get("error") {
                Some(Value::Str(e)) => assert_eq!(got.unwrap_err(), *e),
                _ => assert_eq!(got.unwrap().map(Value::Str).unwrap_or(Value::Null), *c.get("out").unwrap()),
            }
        }
    }

    #[test]
    fn helpers() {
        assert_eq!(thousands(100000), "100,000");
        assert_eq!(thousands(999), "999");
        assert_eq!(py_repr("it's"), "\"it's\"");
        assert_eq!(py_repr("a'b\"c"), "'a\\'b\"c'");
        assert!(iso_date("2024-02-29").is_some() && iso_date("2023-02-29").is_none() && iso_date("2024-2-01").is_none());
        assert_eq!(collapse("  a \t b\u{3000}c "), "a b c");
    }
}
