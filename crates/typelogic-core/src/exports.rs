//! Port of `typelogic/exports.py`: record fingerprints, verification, Lite export and short filenames.
use crate::error::{CoreError, Result};
use crate::hash::{base32, hex, md5, sha256, sha512};
use crate::json::{dumps, DumpOptions, Map, Value};
use crate::text::py_len;

pub const FIELDS: [&str; 6] = ["id", "space", "title", "created", "request", "response"];

/// `exports.canonical_bytes(record)`.
pub fn canonical_bytes(record: &Map) -> Result<String> {
    let mut payload = Map::with_capacity(FIELDS.len());
    for key in FIELDS {
        let v = record.get(key).ok_or_else(|| CoreError::unsupported(format!("record lacks {key}")))?;
        payload.push_unique(key, v.clone());
    }
    dumps(&Value::Object(payload), DumpOptions::COMPACT_SORTED)
}

/// `exports.digests(record)` -> (sha512, md5) hex.
pub fn digests(record: &Map) -> Result<(String, String)> {
    let data = canonical_bytes(record)?;
    Ok((hex(&sha512(data.as_bytes())), hex(&md5(data.as_bytes()))))
}

fn ct_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    let mut diff = (a.len() ^ b.len()) as u8 | ((a.len() != b.len()) as u8);
    for i in 0..a.len().min(b.len()) {
        diff |= a[i] ^ b[i];
    }
    diff == 0
}

fn stored(record: &Map, key: &str) -> Result<String> {
    // str(record.get(key) or '')
    match record.get(key) {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::Str(s)) => Ok(s.clone()),
        Some(_) => Err(CoreError::unsupported("non-text stored digest")),
    }
}

/// `exports.verification(record)`.
pub fn verification(record: &Map) -> Result<Map> {
    let (sha, m5) = digests(record)?;
    let sha_ok = ct_eq(&stored(record, "sha512")?, &sha);
    let md5_ok = ct_eq(&stored(record, "md5")?, &m5);
    let mut out = Map::with_capacity(9);
    out.push_unique("format", Value::str("typelogic-record-v1"));
    out.push_unique("sha512", record.get("sha512").cloned().unwrap_or(Value::Null));
    out.push_unique("md5", record.get("md5").cloned().unwrap_or(Value::Null));
    out.push_unique("sha512_matches", Value::Bool(sha_ok));
    out.push_unique("md5_matches", Value::Bool(md5_ok));
    out.push_unique("verified", Value::Bool(sha_ok && md5_ok));
    out.push_unique("baseline_origin", record.get("baseline_origin").cloned().unwrap_or(Value::str("unknown")));
    out.push_unique("scope", Value::str("Immutable id, space, title, created, request and response. Checksums are not signatures."));
    Ok(out)
}

fn int_key(key: &str) -> Result<i128> {
    // int(key) for the score level IDs ('0', '1', ...).
    key.trim().parse::<i128>().map_err(|_| CoreError::unsupported("non-integer score key"))
}

/// `exports.lite(record)` for well-formed records; anything unusual is unsupported (Python fallback).
pub fn lite(record: &Map) -> Result<Map> {
    let unsupported = |w: &str| CoreError::unsupported(format!("lite: {w}"));
    let request = record.get("request").and_then(Value::as_object).ok_or_else(|| unsupported("request"))?;
    let empty = Map::new();
    let questions = match request.get("questions") {
        None => &empty,
        Some(Value::Object(m)) => m,
        Some(_) => return Err(unsupported("questions")),
    };
    let answers = record
        .get("response")
        .and_then(Value::as_object)
        .and_then(|r| r.get("answers"))
        .and_then(Value::as_object)
        .ok_or_else(|| unsupported("answers"))?;
    let mut rows = Vec::with_capacity(answers.len());
    for (key, answer) in answers.iter() {
        let a = answer.as_object().ok_or_else(|| unsupported("answer"))?;
        let kind = a.get("type").and_then(Value::as_str).ok_or_else(|| unsupported("type"))?;
        let value = match kind {
            "choice" => a.get("choice").cloned().ok_or_else(|| unsupported("choice"))?,
            "noul" => match a.get("noul") {
                Some(Value::Float(f)) => Value::Bool(*f > 0.5),
                Some(Value::Int(s)) => Value::Bool(crate::json::int_to_f64(s)? > 0.5),
                _ => return Err(unsupported("noul")),
            },
            _ => {
                let probs = a.get("probabilities").and_then(Value::as_object).ok_or_else(|| unsupported("probabilities"))?;
                let mut winner: Option<(&String, f64, i128)> = None;
                for (k, v) in probs.iter() {
                    let p = match v { Value::Float(f) => *f, Value::Int(s) => crate::json::int_to_f64(s)?, _ => return Err(unsupported("probability")) };
                    if p.is_nan() {
                        return Err(unsupported("nan probability"));
                    }
                    let neg = -int_key(k)?;
                    // max(..., key=(p, -int(key))): first maximal element wins.
                    let better = match winner { None => true, Some((_, bp, bn)) => p > bp || (p == bp && neg > bn) };
                    if better {
                        winner = Some((k, p, neg));
                    }
                }
                let (winner, _, _) = winner.ok_or_else(|| unsupported("empty probabilities"))?;
                match a.get("legend") {
                    None => Value::str(winner),
                    Some(Value::Object(legend)) => legend.get(winner).cloned().unwrap_or_else(|| Value::str(winner)),
                    Some(_) => return Err(unsupported("legend")),
                }
            }
        };
        let instructions = a.get("instructions").filter(|v| v.truthy()).cloned();
        let question = match instructions {
            Some(v) => v,
            None => match questions.get(key) {
                None => Value::str(key),
                Some(Value::Object(q)) => q.get("instructions").cloned().unwrap_or_else(|| Value::str(key)),
                Some(_) => return Err(unsupported("question")),
            },
        };
        let mut row = Map::with_capacity(2);
        row.push_unique("question", question);
        row.push_unique("answer", value);
        rows.push(Value::Object(row));
    }
    // tags: request.get('tags') or [] (a list, or absent / null / empty); anything else uses the Python fallback.
    let tags = match request.get("tags") {
        None | Some(Value::Null) => Value::Array(Vec::new()),
        Some(Value::Array(items)) => Value::Array(items.clone()),
        Some(_) => return Err(unsupported("tags")),
    };
    let mut out = Map::with_capacity(3);
    out.push_unique("title", record.get("title").cloned().ok_or_else(|| unsupported("title"))?);
    out.push_unique("tags", tags);
    out.push_unique("questions", Value::Array(rows));
    Ok(out)
}

/// `exports.export_filename(decision_id, format)`.
pub fn export_filename(decision_id: &str, format: &str) -> Result<String> {
    if !(1..=128).contains(&py_len(decision_id)) || !(format == "full" || format == "lite") {
        return Err(CoreError::value("Invalid decision ID or format"));
    }
    let digest = sha256(format!("{decision_id}:{format}").as_bytes());
    Ok(base32(&digest[..5]).to_lowercase() + ".json")
}
