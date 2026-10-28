//! LAYA 2.0 router contract: `{"language","task","latency"}` -> `{"model"}` over locally installed profiles.
//! Routing never downloads, never selects an unavailable profile and reports why each candidate was skipped.
use crate::error::{CoreError, Result};
use crate::json::{Map, Value};

#[derive(Debug, Clone, PartialEq)]
pub struct Profile {
    pub name: String,
    pub tasks: Vec<String>,
    /// Primary language subtags (`en`, `pt`) or `*`.
    pub languages: Vec<String>,
    /// `low`, `medium` or `high` expected latency class on this machine.
    pub latency: String,
    pub available: bool,
    pub priority: i64,
}

fn latency_rank(s: &str) -> Option<u8> {
    match s { "low" => Some(0), "medium" => Some(1), "high" => Some(2), _ => None }
}

/// Dominant script of a text sample (letters only), as an ISO 15924 code.
pub fn detect_script(text: &str) -> &'static str {
    let mut counts = [0usize; 10];
    const NAMES: [&str; 10] = ["Latn", "Cyrl", "Grek", "Arab", "Hebr", "Deva", "Hani", "Hang", "Kana", "Thai"];
    for c in text.chars().filter(|c| c.is_alphabetic()) {
        let cp = c as u32;
        let i = match cp {
            0x0041..=0x024F | 0x1E00..=0x1EFF => 0,
            0x0400..=0x052F => 1,
            0x0370..=0x03FF | 0x1F00..=0x1FFF => 2,
            0x0600..=0x06FF | 0x0750..=0x077F | 0x08A0..=0x08FF => 3,
            0x0590..=0x05FF => 4,
            0x0900..=0x097F => 5,
            0x4E00..=0x9FFF | 0x3400..=0x4DBF | 0x20000..=0x2A6DF => 6,
            0xAC00..=0xD7AF | 0x1100..=0x11FF => 7,
            0x3040..=0x30FF => 8,
            0x0E00..=0x0E7F => 9,
            _ => continue,
        };
        counts[i] += 1;
    }
    let (best, n) = counts.iter().enumerate().max_by_key(|(i, n)| (**n, std::cmp::Reverse(*i))).unwrap();
    if *n == 0 { "Zyyy" } else { NAMES[best] }
}

pub fn parse_profiles(v: &Value) -> Result<Vec<Profile>> {
    let items = v.as_array().ok_or_else(|| CoreError::value("profiles must be a list"))?;
    let strings = |m: &Map, k: &str| -> Result<Vec<String>> {
        match m.get(k) {
            Some(Value::Array(a)) => a.iter().map(|x| x.as_str().map(|s| s.to_lowercase()).ok_or_else(|| CoreError::value(format!("profile.{k} must list strings")))).collect(),
            _ => Err(CoreError::value(format!("profile.{k} must be a list"))),
        }
    };
    items
        .iter()
        .map(|item| {
            let m = item.as_object().ok_or_else(|| CoreError::value("profile must be an object"))?;
            let name = m.get("name").and_then(Value::as_str).filter(|s| !s.is_empty()).ok_or_else(|| CoreError::value("profile.name required"))?;
            let latency = m.get("latency").and_then(Value::as_str).unwrap_or("medium").to_owned();
            if latency_rank(&latency).is_none() {
                return Err(CoreError::value("profile.latency must be low, medium or high"));
            }
            let priority = match m.get("priority") { None => 0, Some(Value::Int(s)) => s.parse().unwrap_or(0), _ => return Err(CoreError::value("profile.priority must be an integer")) };
            Ok(Profile {
                name: name.to_owned(),
                tasks: strings(m, "tasks")?,
                languages: strings(m, "languages")?,
                latency,
                available: matches!(m.get("available"), Some(Value::Bool(true)) | None),
                priority,
            })
        })
        .collect()
}

/// Route one request. Output: `{"model": name|null, "language", "script", "candidates": [...], "skipped": [...]}`.
pub fn route(request: &Value, profiles: &[Profile]) -> Result<Map> {
    let req = request.as_object().ok_or_else(|| CoreError::value("Router request must be an object"))?;
    let task = req.get("task").and_then(Value::as_str).ok_or_else(|| CoreError::value("Router request requires task"))?.to_lowercase();
    let latency = req.get("latency").and_then(Value::as_str).unwrap_or("medium");
    let wanted = latency_rank(latency).ok_or_else(|| CoreError::value("latency must be low, medium or high"))?;
    let script = req.get("text").and_then(Value::as_str).map(detect_script);
    let language = match req.get("language").and_then(Value::as_str) {
        Some(l) if !l.trim().is_empty() => l.trim().to_lowercase(),
        _ => match script { Some("Latn") | None => "und".into(), Some(s) => format!("und-{}", s.to_lowercase()) },
    };
    let primary = language.split(['-', '_']).next().unwrap_or("und").to_owned();
    let mut ok: Vec<&Profile> = Vec::new();
    let mut skipped = Vec::new();
    for p in profiles {
        let reason = if !p.available {
            Some("not installed")
        } else if !p.tasks.iter().any(|t| *t == task || t == "*") {
            Some("task not supported")
        } else if !p.languages.iter().any(|l| *l == primary || l == "*") {
            Some("language not covered")
        } else if latency_rank(&p.latency).unwrap() > wanted {
            Some("latency budget exceeded")
        } else {
            None
        };
        match reason {
            Some(r) => {
                let mut m = Map::new();
                m.push_unique("model", Value::str(&p.name));
                m.push_unique("reason", Value::str(r));
                skipped.push(Value::Object(m));
            }
            None => ok.push(p),
        }
    }
    ok.sort_by(|a, b| b.priority.cmp(&a.priority).then_with(|| latency_rank(&a.latency).cmp(&latency_rank(&b.latency))).then_with(|| a.name.cmp(&b.name)));
    let mut out = Map::new();
    out.push_unique("model", ok.first().map(|p| Value::str(&p.name)).unwrap_or(Value::Null));
    out.push_unique("task", Value::Str(task));
    out.push_unique("language", Value::Str(language));
    out.push_unique("script", script.map(Value::str).unwrap_or(Value::Null));
    out.push_unique("latency", Value::str(latency));
    out.push_unique("candidates", Value::Array(ok.iter().map(|p| Value::str(&p.name)).collect()));
    out.push_unique("skipped", Value::Array(skipped));
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::loads;

    #[test]
    fn routes_to_best_available_profile() {
        let profiles = parse_profiles(&loads(r#"[
            {"name":"laya-small","tasks":["classification"],"languages":["en"],"latency":"low","priority":1},
            {"name":"typelogic-qwen35-2b","tasks":["classification","decision"],"languages":["en"],"latency":"medium","priority":5},
            {"name":"multilingual","tasks":["classification"],"languages":["*"],"latency":"high","available":false}]"#, false).unwrap()).unwrap();
        let r = route(&loads(r#"{"language":"en","task":"classification","latency":"low"}"#, false).unwrap(), &profiles).unwrap();
        assert_eq!(r.get("model"), Some(&Value::str("laya-small")));
        let r = route(&loads(r#"{"language":"en-US","task":"decision"}"#, false).unwrap(), &profiles).unwrap();
        assert_eq!(r.get("model"), Some(&Value::str("typelogic-qwen35-2b")));
        let r = route(&loads(r#"{"task":"classification","text":"Привет мир","latency":"high"}"#, false).unwrap(), &profiles).unwrap();
        assert_eq!(r.get("model"), Some(&Value::Null));
        assert_eq!(r.get("script"), Some(&Value::str("Cyrl")));
    }
}
