//! Port of `typelogic/schema.py`: request validation and typed answer construction.
//! Messages, ordering and floating-point operations follow the Python reference exactly;
//! `tests/test_core_parity.py` compares both implementations.
use crate::error::{CoreError, Result};
use crate::json::{dumps, DumpOptions, Map, Value};
use crate::pysum::py_sum;
use crate::text::{py_len, strip};

#[derive(Debug, Clone, PartialEq)]
pub struct Question {
    pub id: String,
    pub kind: String,
    pub instr: String,
    pub keys: Vec<String>,
    pub options: Vec<String>,
    pub criteria: Value,
    pub abstain_option: Option<String>,
}

impl Question {
    /// The dict produced by `schema.validate` for this question (same key order).
    pub fn to_value(&self) -> Value {
        let mut m = Map::with_capacity(7);
        m.push_unique("id", Value::str(&self.id));
        m.push_unique("type", Value::str(&self.kind));
        m.push_unique("instr", Value::str(&self.instr));
        m.push_unique("keys", Value::Array(self.keys.iter().map(Value::str).collect()));
        m.push_unique("options", Value::Array(self.options.iter().map(Value::str).collect()));
        m.push_unique("criteria", self.criteria.clone());
        m.push_unique("abstain_option", self.abstain_option.as_ref().map(Value::str).unwrap_or(Value::Null));
        Value::Object(m)
    }

    /// Rebuild from the dict form (as returned by `validate`). Unexpected shapes are unsupported
    /// so callers fall back to the Python reference.
    pub fn from_value(v: &Value) -> Result<Self> {
        let m = v.as_object().ok_or_else(|| CoreError::unsupported("question must be a dict"))?;
        let s = |k: &str| m.get(k).and_then(Value::as_str).map(str::to_owned).ok_or_else(|| CoreError::unsupported(format!("question.{k}")));
        let list = |k: &str| -> Result<Vec<String>> {
            m.get(k)
                .and_then(Value::as_array)
                .ok_or_else(|| CoreError::unsupported(format!("question.{k}")))?
                .iter()
                .map(|x| x.as_str().map(str::to_owned).ok_or_else(|| CoreError::unsupported(format!("question.{k}[]"))))
                .collect()
        };
        let abstain_option = match m.get("abstain_option") {
            None | Some(Value::Null) => None,
            Some(Value::Str(s)) => Some(s.clone()),
            Some(_) => return Err(CoreError::unsupported("question.abstain_option")),
        };
        Ok(Self {
            id: s("id")?,
            kind: s("type")?,
            instr: s("instr")?,
            keys: list("keys")?,
            options: list("options")?,
            criteria: m.get("criteria").cloned().unwrap_or(Value::Null),
            abstain_option,
        })
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Validated {
    pub state: String,
    pub questions: Vec<Question>,
    pub threshold: f64,
}

/// `schema.canonical(value)`.
pub fn canonical(value: &Value) -> Result<String> { dumps(value, DumpOptions::CANONICAL) }

fn is_number(v: &Value) -> bool { matches!(v, Value::Int(_) | Value::Float(_)) }

/// `schema.validate(payload)`.
pub fn validate(payload: &Value) -> Result<Validated> {
    let p = match payload {
        Value::Object(m) if m.contains_key("state") => m,
        _ => return Err(CoreError::value("Provide an object with state and questions")),
    };
    let state = match p.get("state").expect("checked") {
        Value::Str(s) => s.clone(),
        other => canonical(other)?,
    };
    if strip(&state).is_empty() || py_len(&state) > 120_000 {
        return Err(CoreError::value("Scenario: 1–120,000 characters"));
    }
    let source = match p.get("questions") {
        Some(Value::Object(m)) if (1..=32).contains(&m.len()) => m,
        _ => return Err(CoreError::value("Provide 1–32 questions")),
    };
    let mut questions = Vec::with_capacity(source.len());
    for (key, q) in source.iter() {
        let q = match q {
            Value::Object(q) if (1..=120).contains(&py_len(key)) => q,
            _ => return Err(CoreError::value("Invalid question")),
        };
        let kind = q.get("type").unwrap_or(&Value::Null);
        let instr = q.get("instructions").unwrap_or(&Value::Null);
        let criteria = q.get("criteria").cloned().unwrap_or(Value::Null);
        let kind = match kind.as_str() {
            Some(k @ ("noul" | "choice" | "score")) => k.to_owned(),
            _ => return Err(CoreError::value("Invalid type or instruction")),
        };
        let instr = match instr {
            Value::Str(s) if !strip(s).is_empty() && py_len(s) <= 6000 => s.clone(),
            _ => return Err(CoreError::value("Invalid type or instruction")),
        };
        let (keys, options, criteria) = match kind.as_str() {
            "noul" => {
                let criteria = if criteria.truthy() { criteria } else { Value::Object(Map::new()) };
                let m = match &criteria {
                    Value::Object(m) => m,
                    _ => return Err(CoreError::value("Invalid binary criteria")),
                };
                for (k, v) in m.iter() {
                    let ok_key = k == "false" || k == "true";
                    let ok_val = matches!(v, Value::Str(s) if py_len(s) <= 3000);
                    if !ok_key || !ok_val {
                        return Err(CoreError::value("Invalid binary criteria"));
                    }
                }
                let label = |name: &str, key: &str| match m.get(key) {
                    Some(Value::Str(s)) if !s.is_empty() => format!("{name}: {s}"),
                    _ => name.to_owned(),
                };
                let options = vec![label("no", "false"), label("yes", "true")];
                (vec!["false".to_owned(), "true".to_owned()], options, criteria)
            }
            "choice" => {
                let mut criteria = criteria;
                if let Value::Array(items) = &criteria {
                    if items.iter().all(|v| matches!(v, Value::Str(_))) {
                        let names: Vec<&str> = items.iter().map(|v| v.as_str().unwrap()).collect();
                        let mut seen = std::collections::HashSet::new();
                        if !names.iter().all(|n| seen.insert(*n)) {
                            return Err(CoreError::value("Duplicate alternatives"));
                        }
                        let mut m = Map::with_capacity(names.len());
                        for n in names {
                            m.push_unique(n, Value::str(""));
                        }
                        criteria = Value::Object(m);
                    }
                }
                let m = match &criteria {
                    Value::Object(m) if (2..=255).contains(&m.len()) => m,
                    _ => return Err(CoreError::value("Choice: 2–255 alternatives")),
                };
                for (k, v) in m.iter() {
                    let bad_key = strip(k).is_empty() || py_len(k) > 200;
                    let bad_val = match v {
                        Value::Null => false,
                        Value::Str(s) => !s.is_empty() && py_len(s) > 3000,
                        _ => true,
                    };
                    if bad_key || bad_val {
                        return Err(CoreError::value("Invalid alternative"));
                    }
                }
                let mut normalized = Map::with_capacity(m.len());
                let mut keys = Vec::with_capacity(m.len());
                let mut options = Vec::with_capacity(m.len());
                for (k, v) in m.iter() {
                    let text = v.as_str().unwrap_or("").to_owned();
                    options.push(if text.is_empty() { k.clone() } else { format!("{k}: {text}") });
                    keys.push(k.clone());
                    normalized.push_unique(k.clone(), Value::Str(text));
                }
                (keys, options, Value::Object(normalized))
            }
            _ => {
                let levels = match &criteria {
                    Value::Array(items) if (2..=32).contains(&items.len()) => items,
                    _ => return Err(CoreError::value("Score: 2–32 unique ordered levels")),
                };
                let mut seen = std::collections::HashSet::new();
                let mut options = Vec::with_capacity(levels.len());
                for v in levels {
                    match v {
                        Value::Str(s) if !strip(s).is_empty() && py_len(s) <= 3000 => {
                            seen.insert(s.as_str());
                            options.push(s.clone());
                        }
                        _ => return Err(CoreError::value("Score: 2–32 unique ordered levels")),
                    }
                }
                if seen.len() != levels.len() {
                    return Err(CoreError::value("Score: 2–32 unique ordered levels"));
                }
                ((0..levels.len()).map(|i| i.to_string()).collect(), options, criteria.clone())
            }
        };
        let abstain_option = match q.get("abstain_option") {
            None | Some(Value::Null) => None,
            Some(v) => match v {
                Value::Str(s) if kind == "choice" && keys.iter().any(|k| k == s) => Some(s.clone()),
                _ => return Err(CoreError::value("abstain_option must identify a declared Choice alternative")),
            },
        };
        questions.push(Question { id: key.clone(), kind, instr, keys, options, criteria, abstain_option });
    }
    let threshold = match p.get("threshold") {
        None => 0.85,
        Some(v) if is_number(v) => {
            let t = v.to_f64()?; // OverflowError for huge ints, like math.isfinite(int)
            if !t.is_finite() || !(0.5..=1.0).contains(&t) || !int_threshold_exact(v, t) {
                return Err(CoreError::value("Threshold: 0.5–1"));
            }
            t
        }
        Some(_) => return Err(CoreError::value("Threshold: 0.5–1")),
    };
    match p.get("permutation_check") {
        None | Some(Value::Bool(_)) => {}
        Some(_) => return Err(CoreError::value("permutation_check must be a boolean")),
    }
    Ok(Validated { state, questions, threshold })
}

/// Python compares ints with floats exactly; within [0.5, 1] the only int is 1.
fn int_threshold_exact(v: &Value, t: f64) -> bool {
    match v {
        Value::Int(s) => s == "1" && t == 1.0,
        _ => true,
    }
}

/// `schema.answers(questions, rows, threshold, calibration)`.
pub fn answers(questions: &[Question], rows: &[Vec<f64>], threshold: f64, calibration: &Value) -> Result<Map> {
    if questions.len() != rows.len() {
        return Err(CoreError::runtime("Inconsistent answer count"));
    }
    let mut output = Map::with_capacity(questions.len());
    for (q, row) in questions.iter().zip(rows) {
        let p = row;
        let total = py_sum(p.iter().copied());
        if p.len() != q.keys.len() || p.iter().any(|v| !v.is_finite() || *v < 0.0) || total <= 0.0 {
            return Err(CoreError::runtime("Invalid distribution"));
        }
        if !total.is_finite() {
            return Err(CoreError::runtime("Invalid distribution total"));
        }
        let p: Vec<f64> = p.iter().map(|v| v / total).collect();
        let mut best = 0usize;
        for (i, v) in p.iter().enumerate() {
            if *v > p[best] {
                best = i;
            }
        }
        let top = p[best];
        let mut reasons = vec![Value::str("human_review_required")];
        if top < threshold {
            reasons.push(Value::str("below_threshold"));
        }
        if q.kind == "choice" && q.abstain_option.is_none() {
            reasons.push(Value::str("abstention_not_declared"));
        }
        let abstained = q.abstain_option.as_deref().map(|a| q.keys[best] == a).unwrap_or(false);
        if abstained {
            reasons.push(Value::str("explicit_abstention"));
        }
        let n = p.len() as f64;
        let mut a = Map::with_capacity(16);
        a.push_unique("type", Value::str(&q.kind));
        a.push_unique("instructions", Value::str(&q.instr));
        a.push_unique("criteria", q.criteria.clone());
        let mut probs = Map::with_capacity(p.len());
        for (k, v) in q.keys.iter().zip(&p) {
            probs.insert(k.clone(), Value::Float(*v));
        }
        a.push_unique("probabilities", Value::Object(probs));
        a.push_unique("concentration", Value::Float((top - 1.0 / n) / (1.0 - 1.0 / n)));
        a.push_unique("concentration_is_correctness_probability", Value::Bool(false));
        a.push_unique("calibration", calibration.clone());
        a.push_unique("abstained", Value::Bool(abstained));
        a.push_unique("requires_review", Value::Bool(true));
        a.push_unique("eligible_for_automation", Value::Bool(false));
        a.push_unique("review_reasons", Value::Array(reasons));
        a.push_unique("recommendation", Value::str(if abstained || top < threshold { "review" } else { "candidate" }));
        match q.kind.as_str() {
            "choice" => a.push_unique("choice", Value::str(&q.keys[best])),
            "noul" => a.push_unique("noul", Value::Float(p[1])),
            _ => {
                let score = py_sum(p.iter().enumerate().map(|(i, v)| (i as f64) * v));
                a.push_unique("score", Value::Float(score));
                let mut legend = Map::with_capacity(q.keys.len());
                for (k, o) in q.keys.iter().zip(&q.options) {
                    legend.insert(k.clone(), Value::str(o));
                }
                a.push_unique("legend", Value::Object(legend));
            }
        }
        output.insert(q.id.clone(), Value::Object(a));
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::loads;

    #[test]
    fn validates_like_python() {
        let p = loads(r#"{"state":{"b":1,"a":[1.5]},"questions":{"route":{"type":"choice","instructions":"Route?","criteria":["A","B"]},
            "ok":{"type":"noul","instructions":"OK?","criteria":{"true":"fine"}},"lvl":{"type":"score","instructions":"Level","criteria":["low","high"]}},"threshold":1}"#, false).unwrap();
        let v = validate(&p).unwrap();
        assert_eq!(v.state, r#"{"a": [1.5], "b": 1}"#);
        assert_eq!(v.threshold, 1.0);
        assert_eq!(v.questions[0].options, vec!["A", "B"]);
        assert_eq!(v.questions[1].options, vec!["no", "yes: fine"]);
        assert_eq!(v.questions[2].keys, vec!["0", "1"]);
        let rows = vec![vec![0.2, 0.8], vec![1.0, 3.0], vec![0.5, 0.5]];
        let out = answers(&v.questions, &rows, v.threshold, &Value::str("unvalidated")).unwrap();
        let route = out.get("route").unwrap().as_object().unwrap();
        assert_eq!(route.get("choice"), Some(&Value::str("B")));
        assert_eq!(route.get("recommendation"), Some(&Value::str("review")));
    }

    #[test]
    fn rejects_with_python_messages() {
        let bad = |s: &str| validate(&loads(s, false).unwrap()).unwrap_err().message;
        assert_eq!(bad(r#"{"state":" ","questions":{}}"#), "Scenario: 1–120,000 characters");
        assert_eq!(bad(r#"{"state":"x","questions":{"q":{"type":"choice","instructions":"i","criteria":["A","A"]}}}"#), "Duplicate alternatives");
        assert_eq!(bad(r#"{"state":"x","questions":{"q":{"type":"noul","instructions":"i","abstain_option":"true"}}}"#), "abstain_option must identify a declared Choice alternative");
        assert_eq!(bad(r#"{"state":"x","questions":{"q":{"type":"noul","instructions":"i"}},"threshold":true}"#), "Threshold: 0.5–1");
    }
}
