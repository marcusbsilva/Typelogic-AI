//! Decision templates (port of `typelogic/templates.py`): an example case, suggested tags, an evidence schema and
//! decisions. Built-in templates are embedded in TypeLogic.exe (templates/builtin); templates saved from the app go
//! to `data/templates/<id>.json` and replace a built-in template of the same id only when their id differs.
use crate::casefmt;
use crate::util::write_atomic;
use std::path::Path;
use typelogic_core::json::{dumps_indent, loads, DumpOptions, Map, Value};
use typelogic_core::text::{py_len, strip};

include!(concat!(env!("OUT_DIR"), "/builtin_templates.rs"));

pub const FORMAT: &str = "typelogic-template-v1";
const FIELDS: [&str; 11] = ["id", "title", "description", "case", "tags", "evidence_schema", "evidence", "decisions", "threshold", "permutation_check", "example"];
const LEGACY: [&str; 2] = ["rules", "exact_checks"];
const LEGACY_DECISION: [&str; 2] = ["auto_review", "verdict"];

type R<T> = Result<T, String>;

/// `^[a-z0-9][a-z0-9-]{0,63}$`
fn is_id(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty() && b.len() <= 64 && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit()) && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

/// A case request built from a template (`to_request`); values are marked as coming from the template.
pub fn to_request(t: &Map, include_values: bool) -> R<Map> {
    let case = match t.get("case") {
        Some(v) if v.truthy() => match v {
            Value::Str(s) => s.clone(),
            other => casefmt::case_text(Some(other))?,
        },
        _ => String::new(),
    };
    let mut r = Map::new();
    r.push_unique("template", t.get("id").cloned().unwrap_or(Value::Null));
    r.push_unique("case", Value::Str(if include_values { case } else { String::new() }));
    r.push_unique("evidence_schema", t.get("evidence_schema").cloned().unwrap_or(Value::Array(Vec::new())));
    r.push_unique("decisions", t.get("decisions").cloned().unwrap_or(Value::Null));
    r.push_unique("threshold", t.get("threshold").cloned().unwrap_or(Value::Float(0.85)));
    r.push_unique("permutation_check", t.get("permutation_check").cloned().unwrap_or(Value::Bool(true)));
    if let Some(tags) = t.get("tags").filter(|v| v.truthy()) {
        r.push_unique("tags", tags.clone());
    }
    let mut evidence = Map::new();
    if include_values {
        if let Some(Value::Object(values)) = t.get("evidence") {
            for (k, v) in values.iter() {
                let mut e = Map::new();
                e.push_unique("value", v.clone());
                e.push_unique("source", Value::str("template"));
                e.push_unique("status", Value::str("confirmed"));
                evidence.push_unique(k.clone(), Value::Object(e));
            }
        }
    }
    r.push_unique("evidence", Value::Object(evidence));
    Ok(r)
}

/// Checked copy of a template (`validate`); the first problem as the error.
pub fn validate(template: &Value) -> R<Map> {
    let t = template.as_object().ok_or("A template must be a JSON object")?;
    let mut unknown: Vec<&String> = t.keys().filter(|k| !FIELDS.contains(&k.as_str()) && !LEGACY.contains(&k.as_str()) && k.as_str() != "format").collect();
    if !unknown.is_empty() {
        unknown.sort();
        return Err(format!("Unknown template keys: {}", unknown.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", ")));
    }
    let id = t.get("id").and_then(Value::as_str).filter(|i| is_id(i)).ok_or("Template id: lowercase letters, digits and -, up to 64 characters")?;
    let title = t.get("title").and_then(Value::as_str).filter(|s| !strip(s).is_empty() && py_len(s) <= 200).ok_or("Template title: 1-200 characters")?;
    let description = match t.get("description") {
        None => String::new(),
        Some(v) if !v.truthy() => String::new(),
        Some(Value::Str(d)) if py_len(d) <= 1000 => d.clone(),
        _ => return Err("Template description: up to 1,000 characters".into()),
    };
    let mut out = Map::new();
    out.push_unique("format", Value::str(FORMAT));
    out.push_unique("id", Value::str(id));
    out.push_unique("title", Value::str(strip(title)));
    out.push_unique("description", Value::str(strip(&description)));
    for key in ["case", "tags", "evidence_schema", "evidence", "decisions", "threshold", "permutation_check", "example"] {
        if let Some(v) = t.get(key) {
            out.push_unique(key, v.clone());
        }
    }
    if !out.contains_key("decisions") {
        return Err("A template needs at least one decision".into());
    }
    if let Some(case) = out.get("case").cloned() {
        if case.truthy() && case.as_str().is_none() {
            out.insert("case", Value::Str(casefmt::case_text(Some(&case))?));
        }
    }
    if let Some(Value::Array(ds)) = out.get("decisions").cloned() {
        let ds = ds
            .into_iter()
            .map(|d| match d {
                Value::Object(m) => Value::Object(m.into_entries().into_iter().filter(|(k, _)| !LEGACY_DECISION.contains(&k.as_str())).fold(Map::new(), |mut acc, (k, v)| {
                    acc.push_unique(k, v);
                    acc
                })),
                other => other,
            })
            .collect();
        out.insert("decisions", Value::Array(ds));
    }
    if let Some(tags) = out.get("tags").cloned() {
        out.insert("tags", Value::Array(casefmt::validate_tags(Some(&tags))?.into_iter().map(Value::Str).collect()));
    }
    let mut request = to_request(&out, true)?;
    if !request.get("case").is_some_and(Value::truthy) {
        request.insert("case", out.get("title").cloned().unwrap_or(Value::Null));
    }
    casefmt::normalize(&request)?; // same checks as a real request
    Ok(out)
}

fn row(t: Map, origin: &str, path: String) -> Value {
    let mut m = Map::new();
    for k in ["id", "title", "description"] {
        m.push_unique(k, t.get(k).cloned().unwrap_or(Value::Null));
    }
    m.push_unique("origin", Value::str(origin));
    m.push_unique("path", Value::Str(path));
    m.push_unique("template", Value::Object(t));
    Value::Object(m)
}

/// `{templates: [{id, title, description, origin, path, template}], errors}`: built-in first, then by title.
pub fn load_all(data_dir: &Path) -> Value {
    let mut found: Vec<(String, Value)> = Vec::new();
    let mut errors = Vec::new();
    let mut put = |id: String, v: Value| match found.iter().position(|(i, _)| *i == id) {
        Some(i) => found[i] = (id, v),
        None => found.push((id, v)),
    };
    let mut error = |path: String, e: String| {
        let mut m = Map::new();
        m.push_unique("path", Value::Str(path));
        m.push_unique("error", Value::Str(e.chars().take(300).collect()));
        errors.push(Value::Object(m));
    };
    for (name, text) in BUILTIN {
        match loads(text, false).map_err(|e| e.message).and_then(|v| validate(&v)) {
            Ok(t) => {
                let id = t.get("id").and_then(Value::as_str).unwrap_or("").to_owned();
                put(id, row(t, "builtin", format!("built in: {name}")));
            }
            Err(e) => error(format!("built in: {name}"), e),
        }
    }
    let folder = data_dir.join("templates");
    let mut paths: Vec<_> = std::fs::read_dir(&folder).map(|d| d.filter_map(|e| e.ok()).map(|e| e.path()).collect()).unwrap_or_default();
    paths.sort();
    for path in paths.into_iter().filter(|p| p.extension().is_some_and(|x| x == "json")) {
        let text = std::fs::read_to_string(&path);
        match text.map_err(|e| e.to_string()).and_then(|t| loads(&t, false).map_err(|e| e.message)).and_then(|v| validate(&v)) {
            Ok(t) => {
                let id = t.get("id").and_then(Value::as_str).unwrap_or("").to_owned();
                put(id, row(t, "local", path.display().to_string()));
            }
            Err(e) => error(path.display().to_string(), e),
        }
    }
    let mut rows: Vec<Value> = found.into_iter().map(|(_, v)| v).collect();
    let key = |v: &Value| {
        let m = v.as_object();
        let origin = m.and_then(|m| m.get("origin")).and_then(Value::as_str) != Some("builtin");
        (origin, m.and_then(|m| m.get("title")).and_then(Value::as_str).unwrap_or("").to_lowercase())
    };
    rows.sort_by_key(key);
    let mut out = Map::new();
    out.push_unique("templates", Value::Array(rows));
    out.push_unique("errors", Value::Array(errors));
    Value::Object(out)
}

/// Save a user template to `data/templates/<id>.json` (`save`). Built-in ids cannot be replaced.
pub fn save(template: Option<&Value>, data_dir: &Path, overwrite: bool) -> R<Value> {
    let t = validate(template.unwrap_or(&Value::Null))?;
    let id = t.get("id").and_then(Value::as_str).unwrap_or("").to_owned();
    if BUILTIN.iter().any(|(name, _)| *name == format!("{id}.json")) {
        return Err(format!("\"{id}\" is a built-in template; save it under another id"));
    }
    let path = data_dir.join("templates").join(format!("{id}.json"));
    if !overwrite && path.exists() {
        return Err(format!("A template with id \"{id}\" already exists; choose another id or confirm the overwrite"));
    }
    let text = dumps_indent(&Value::Object(t), 2, DumpOptions::REPLY).map_err(|e| e.message)? + "\n";
    write_atomic(&path, text.as_bytes()).map_err(|e| e.to_string())?;
    let mut m = Map::new();
    m.push_unique("id", Value::Str(id));
    m.push_unique("saved", Value::Array(vec![Value::Str(path.display().to_string())]));
    Ok(Value::Object(m))
}

#[cfg(test)]
mod tests {
    use super::*;
    use typelogic_core::json::dumps;

    #[test]
    fn builtin_templates_load_and_save_rules_hold() {
        let dir = std::env::temp_dir().join(format!("tl-tpl-{}", crate::util::random_hex(4)));
        let all = load_all(&dir);
        let all = all.as_object().unwrap();
        assert_eq!(all.get("errors").unwrap().as_array().unwrap().len(), 0);
        let rows = all.get("templates").unwrap().as_array().unwrap();
        assert_eq!(rows.len(), 14);
        let t = rows.iter().find(|r| r.as_object().unwrap().get("id") == Some(&Value::str("billing-duplicate-payment"))).unwrap();
        let mut custom = t.as_object().unwrap().get("template").unwrap().as_object().unwrap().clone();
        assert!(save(Some(&Value::Object(custom.clone())), &dir, false).unwrap_err().contains("built-in"));
        custom.insert("id", Value::str("my-billing"));
        custom.insert("title", Value::str("Zeta billing"));
        save(Some(&Value::Object(custom.clone())), &dir, false).unwrap();
        assert!(save(Some(&Value::Object(custom.clone())), &dir, false).unwrap_err().contains("already exists"));
        save(Some(&Value::Object(custom)), &dir, true).unwrap();
        let all = load_all(&dir);
        let rows = all.as_object().unwrap().get("templates").unwrap().as_array().unwrap().clone();
        assert_eq!(rows.len(), 15);
        assert_eq!(rows.last().unwrap().as_object().unwrap().get("origin"), Some(&Value::str("local")));
        assert_eq!(validate(&loads(r#"{"id":"x","title":"t","decisions":[],"bogus":1}"#, false).unwrap()).unwrap_err(), "Unknown template keys: bogus");
        let _ = dumps(&Value::Null, DumpOptions::REPLY);
        std::fs::remove_dir_all(dir).unwrap();
    }

    /// The same requests as 1.x `templates.to_request` (tests/casefmt_fixture.json holds them, normalized).
    #[test]
    fn requests_match_1x() {
        let fixture = loads(include_str!("../tests/casefmt_fixture.json"), false).unwrap();
        let cases = fixture.as_object().unwrap().get("normalize").unwrap().as_array().unwrap();
        for (name, text) in BUILTIN {
            let t = validate(&loads(text, false).unwrap()).unwrap();
            for values in [true, false] {
                let mut r = to_request(&t, values).unwrap();
                if !r.get("case").is_some_and(Value::truthy) {
                    r.insert("case", t.get("title").cloned().unwrap());
                }
                let want = cases.iter().find(|c| c.as_object().unwrap().get("payload") == Some(&Value::Object(r.clone())));
                assert!(want.is_some(), "{name} values={values}: {}", dumps(&Value::Object(r), DumpOptions::REPLY).unwrap());
            }
        }
    }
}
