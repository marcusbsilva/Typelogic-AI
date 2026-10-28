//! Language-neutral entry point: `call(method, params_json) -> response_json`.
//! Node.js, Lua and the stdio CLI all use this one contract, so every
//! binding exposes identical behaviour. Responses are `{"ok": result}` or
//! `{"error": {"type": "ValueError", "message": "..."}}`.
use crate::error::{CoreError, Result};
use crate::json::{dumps, loads, DumpOptions, Map, Value};
use crate::{exports, hash, router, schema};

pub const METHODS: &[&str] = &[
    "version", "methods", "validate", "answers", "canonical", "digests", "verification", "lite", "export_filename",
    "hash", "route", "detect_script",
];

fn obj(params: &Value) -> Result<&Map> { params.as_object().ok_or_else(|| CoreError::value("params must be an object")) }

fn field<'a>(m: &'a Map, k: &str) -> Result<&'a Value> { m.get(k).ok_or_else(|| CoreError::value(format!("missing parameter: {k}"))) }

fn record(m: &Map) -> Result<&Map> { field(m, "record")?.as_object().ok_or_else(|| CoreError::value("record must be an object")) }

fn s<'a>(m: &'a Map, k: &str) -> Result<&'a str> { field(m, k)?.as_str().ok_or_else(|| CoreError::value(format!("{k} must be a string"))) }

/// Execute one method on parsed parameters.
pub fn dispatch(method: &str, params: &Value) -> Result<Value> {
    match method {
        "version" => {
            let mut m = Map::new();
            m.push_unique("core", Value::str(crate::VERSION));
            m.push_unique("unicode", Value::str(crate::UNICODE_VERSION));
            Ok(Value::Object(m))
        }
        "methods" => Ok(Value::Array(METHODS.iter().map(|m| Value::str(*m)).collect())),
        "validate" => {
            let v = schema::validate(field(obj(params)?, "payload")?)?;
            let mut m = Map::new();
            m.push_unique("state", Value::Str(v.state));
            m.push_unique("questions", Value::Array(v.questions.iter().map(|q| q.to_value()).collect()));
            m.push_unique("threshold", Value::Float(v.threshold));
            Ok(Value::Object(m))
        }
        "answers" => {
            let p = obj(params)?;
            let questions = field(p, "questions")?.as_array().ok_or_else(|| CoreError::value("questions must be a list"))?;
            let questions: Vec<schema::Question> = questions.iter().map(schema::Question::from_value).collect::<Result<_>>()?;
            let rows = field(p, "rows")?.as_array().ok_or_else(|| CoreError::value("rows must be a list"))?;
            let rows: Vec<Vec<f64>> = rows
                .iter()
                .map(|r| r.as_array().ok_or_else(|| CoreError::value("row must be a list"))?.iter().map(Value::to_f64).collect())
                .collect::<Result<_>>()?;
            let threshold = field(p, "threshold")?.to_f64()?;
            Ok(Value::Object(schema::answers(&questions, &rows, threshold, p.get("calibration").unwrap_or(&Value::Null))?))
        }
        "canonical" => {
            let p = obj(params)?;
            let compact = matches!(p.get("compact"), Some(Value::Bool(true)));
            let opts = if compact { DumpOptions::COMPACT_SORTED } else { DumpOptions::CANONICAL };
            Ok(Value::Str(dumps(field(p, "value")?, opts)?))
        }
        "digests" => {
            let (sha512, md5) = exports::digests(record(obj(params)?)?)?;
            let mut m = Map::new();
            m.push_unique("sha512", Value::Str(sha512));
            m.push_unique("md5", Value::Str(md5));
            Ok(Value::Object(m))
        }
        "verification" => Ok(Value::Object(exports::verification(record(obj(params)?)?)?)),
        "lite" => Ok(Value::Object(exports::lite(record(obj(params)?)?)?)),
        "export_filename" => {
            let p = obj(params)?;
            let format = p.get("format").and_then(Value::as_str).unwrap_or("full");
            Ok(Value::Str(exports::export_filename(s(p, "id")?, format)?))
        }
        "hash" => {
            let p = obj(params)?;
            let data = s(p, "text")?.as_bytes();
            let digest = match s(p, "algorithm")? {
                "sha256" => hash::hex(&hash::sha256(data)),
                "sha512" => hash::hex(&hash::sha512(data)),
                "md5" => hash::hex(&hash::md5(data)),
                "blake2s" => hash::hex(&hash::blake2s(data, 32)),
                other => return Err(CoreError::value(format!("Unknown hash algorithm: {other}"))),
            };
            Ok(Value::Str(digest))
        }
        "route" => {
            let p = obj(params)?;
            let profiles = router::parse_profiles(field(p, "profiles")?)?;
            Ok(Value::Object(router::route(field(p, "request")?, &profiles)?))
        }
        "detect_script" => Ok(Value::str(router::detect_script(s(obj(params)?, "text")?))),
        other => Err(CoreError::value(format!("Unknown method: {other}"))),
    }
}

fn error_value(e: &CoreError) -> Value {
    let mut inner = Map::new();
    inner.push_unique("type", Value::str(e.kind_name()));
    inner.push_unique("message", Value::str(&e.message));
    let mut m = Map::new();
    m.push_unique("error", Value::Object(inner));
    Value::Object(m)
}

/// JSON text in, JSON text out. Never panics on bad input.
pub fn call(method: &str, params_json: &str) -> String {
    let result = std::panic::catch_unwind(|| loads(params_json, true).and_then(|p| dispatch(method, &p)));
    let value = match result {
        Ok(Ok(v)) => {
            let mut m = Map::new();
            m.push_unique("ok", v);
            Value::Object(m)
        }
        Ok(Err(e)) => error_value(&e),
        Err(_) => error_value(&CoreError::runtime("internal error in typelogic-core")),
    };
    dumps(&value, DumpOptions::REPLY).unwrap_or_else(|e| dumps(&error_value(&e), DumpOptions::REPLY).expect("error is serializable"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_contract() {
        assert!(call("version", "{}").starts_with("{\"ok\": {\"core\""));
        assert_eq!(call("canonical", r#"{"value":{"b":1,"a":2}}"#), r#"{"ok": "{\"a\": 2, \"b\": 1}"}"#);
        assert!(call("nope", "{}").contains("Unknown method"));
        assert!(call("validate", "not json").contains("\"type\": \"ValueError\""));
    }
}
