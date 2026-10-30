//! Evaluate page: the public JevBench decisions (embedded, MIT) or the user's JSONL, run through the loaded engines.
//!
//! Metrics follow JevBench's conventions (and `tools/jevbench_eval.py` of 1.x): accuracy with failures counted wrong,
//! chance-corrected skill `(accuracy - chance) / (1 - chance)` with chance = mean of 1/k, multi-class Brier, NLL, ECE over
//! ten top-probability bins, ordinal MAE of the expected level for Score questions.
//!
//! Decision Index (0-100): the Intelligence axis of the JevBench score on the public tiers, i.e. the per-tier skill,
//! clipped at 0, averaged with JevBench's tier weights (hard 30, standard 28, easy 14; the public `original` tier
//! stands for standard; the judge tier and the 109 held-out hard items are not public). A user file is one tier.
//! It is not the JevBench leaderboard score (no held-out items, no judge tier, no calibration or cost axis).
//!
//! Bias probes: option order (the two-permutation diagnostic of every Choice answer: top choice changed, largest
//! probability shift), position (first / last option chosen vs gold), polarity (Yes/No: "true" chosen vs gold) and
//! leniency (Score: expected level vs gold level).
use crate::app::Decide;
use crate::engine::Engine;
use crate::util::{now_iso, random_hex, write_atomic};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;
use typelogic_core::json::{dumps, dumps_indent, loads, DumpOptions, Map, Value};

const JEVBENCH: [(&str, &str); 3] = [
    ("original", include_str!("../../../bench/jevbench-public/original.jsonl")),
    ("easy", include_str!("../../../bench/jevbench-public/easy.jsonl")),
    ("hard", include_str!("../../../bench/jevbench-public/hard.jsonl")),
];
pub const DATASET: &str = "JevBench public v1.2 (original 72 + easy 48 + hard 111), MIT, fstandhartinger/jevbench@609a664e";
/// JevBench tier weights of the Intelligence axis (the public `original` tier is weighted as `standard`).
const TIER_WEIGHTS: [(&str, f64); 3] = [("hard", 30.0), ("original", 28.0), ("easy", 14.0)];
const MAX_ITEMS: usize = 5000;
const FORMAT: &str = "typelogic-evaluation-v1";

#[derive(Debug, Clone)]
pub struct Item {
    pub id: String,
    pub tier: String,
    pub family: String,
    pub state: String,
    /// (question id, question as sent to the engine, canonical option keys, gold index)
    pub questions: Vec<(String, Value, Vec<String>, usize)>,
}

/// One answered (or failed) question.
#[derive(Debug, Clone)]
pub struct Row {
    pub id: String,
    pub tier: String,
    pub family: String,
    pub qid: String,
    pub kind: String,
    pub keys: Vec<String>,
    pub gold: usize,
    pub dist: Option<Vec<f64>>,
    pub engine: String,
    pub latency_s: f64,
    pub error: Option<String>,
    pub flip: Option<bool>,
    pub shift: Option<f64>,
}

impl Row {
    fn predicted(&self) -> Option<usize> {
        let d = self.dist.as_ref()?;
        (0..d.len()).fold(None, |best: Option<usize>, i| if best.is_none_or(|b| d[i] > d[b]) { Some(i) } else { best })
    }
    fn correct(&self) -> bool { self.predicted() == Some(self.gold) }

    pub fn to_value(&self) -> Value {
        let mut m = Map::new();
        m.push_unique("id", Value::Str(self.id.clone()));
        m.push_unique("tier", Value::Str(self.tier.clone()));
        m.push_unique("family", Value::Str(self.family.clone()));
        m.push_unique("question", Value::Str(self.qid.clone()));
        m.push_unique("type", Value::Str(self.kind.clone()));
        m.push_unique("gold", Value::Str(self.keys[self.gold].clone()));
        m.push_unique("predicted", self.predicted().map(|i| Value::Str(self.keys[i].clone())).unwrap_or(Value::Null));
        m.push_unique("correct", Value::Bool(self.correct()));
        m.push_unique("valid", Value::Bool(self.dist.is_some()));
        if let Some(d) = &self.dist {
            let mut p = Map::new();
            for (k, v) in self.keys.iter().zip(d) {
                p.push_unique(k.clone(), Value::Float(*v));
            }
            m.push_unique("probabilities", Value::Object(p));
        }
        m.push_unique("engine", Value::Str(self.engine.clone()));
        m.push_unique("latency_s", Value::Float(self.latency_s));
        if let Some(f) = self.flip {
            m.push_unique("order_top_choice_changed", Value::Bool(f));
        }
        if let Some(s) = self.shift {
            m.push_unique("order_max_probability_shift", Value::Float(s));
        }
        if let Some(e) = &self.error {
            m.push_unique("error", Value::Str(e.clone()));
        }
        Value::Object(m)
    }
}

fn get<'a>(v: &'a Value, path: &[&str]) -> Option<&'a Value> { path.iter().try_fold(v, |v, k| v.as_object()?.get(k)) }
fn num(v: Option<&Value>) -> Option<f64> { v.and_then(|v| v.to_f64().ok()) }
fn text(v: Option<&Value>) -> String { v.and_then(Value::as_str).unwrap_or("").to_owned() }

/// Canonical option keys of a question (1.x answers are keyed this way) and the question as the engine takes it
/// (a Choice list becomes `{key: null}`).
fn question_keys(q: &Value) -> Result<(Value, Vec<String>), String> {
    let kind = text(get(q, &["type"]));
    let criteria = get(q, &["criteria"]);
    let keys: Vec<String> = match (kind.as_str(), criteria) {
        ("noul", _) => vec!["false".into(), "true".into()],
        ("choice", Some(Value::Object(m))) => m.keys().cloned().collect(),
        ("choice", Some(Value::Array(a))) => a.iter().map(|v| v.as_str().map(str::to_owned).ok_or("choice options must be strings")).collect::<Result<_, _>>()?,
        ("score", Some(Value::Array(a))) => (0..a.len()).map(|i| i.to_string()).collect(),
        ("score", Some(Value::Object(m))) => (0..m.len()).map(|i| i.to_string()).collect(),
        ("choice" | "score", _) => return Err(format!("a {kind} question needs criteria")),
        _ => return Err("question type must be noul, choice or score".into()),
    };
    if keys.len() < 2 {
        return Err("a question needs at least two options".into());
    }
    let mut out = q.as_object().cloned().ok_or("a question must be an object")?;
    if let (true, Some(Value::Array(_))) = (kind == "choice", criteria) {
        let mut m = Map::new();
        for k in &keys {
            m.push_unique(k.clone(), Value::Null);
        }
        out.insert("criteria", Value::Object(m));
    }
    Ok((Value::Object(out), keys))
}

/// Index of the gold answer among the canonical keys (Yes/No accepts yes/no, true/false; Score the level number).
fn gold_index(kind: &str, keys: &[String], gold: &Value) -> Result<usize, String> {
    let raw = match gold {
        Value::Str(s) => s.trim().to_lowercase(),
        Value::Int(i) => i.clone(),
        Value::Bool(b) => b.to_string(),
        _ => return Err("the gold answer must be a string, a number or a boolean".into()),
    };
    let key = match kind {
        "noul" => match raw.as_str() {
            "yes" | "true" => "true".to_owned(),
            "no" | "false" => "false".to_owned(),
            _ => return Err(format!("gold {raw:?} is not yes/no")),
        },
        "choice" => match gold {
            Value::Str(s) => s.clone(),
            _ => return Err("the gold answer of a choice question is an option key".into()),
        },
        _ => raw,
    };
    keys.iter().position(|k| *k == key).ok_or_else(|| format!("gold {key:?} is not an option of the question"))
}

fn state_text(v: Option<&Value>) -> Result<String, String> {
    match v {
        Some(Value::Str(s)) => Ok(s.clone()),
        Some(v @ (Value::Object(_) | Value::Array(_))) => dumps_indent(v, 1, DumpOptions::REPLY).map_err(|e| e.message),
        _ => Err("missing state (or case)".into()),
    }
}

/// One JSONL row: the JevBench format `{id, state, question, expected}` or the TypeLogic format
/// `{id, state|case, questions: {id: question}, gold: {id: key}}`; `tier` and `family` are optional.
fn parse_row(row: &Value, line: usize, default_tier: &str) -> Result<Item, String> {
    let id = match get(row, &["id"]) {
        Some(Value::Str(s)) if !s.is_empty() => s.clone(),
        Some(Value::Int(i)) => i.clone(),
        None => format!("line-{line}"),
        _ => return Err("id must be a string".into()),
    };
    let state = state_text(get(row, &["state"]).or_else(|| get(row, &["case"])))?;
    let mut questions = Vec::new();
    if let Some(q) = get(row, &["question"]) {
        let (q2, keys) = question_keys(q)?;
        let gold = gold_index(&text(get(q, &["type"])), &keys, get(row, &["expected"]).or_else(|| get(row, &["gold"])).ok_or("missing expected")?)?;
        questions.push(("q".to_owned(), q2, keys, gold));
    } else if let Some(Value::Object(qs)) = get(row, &["questions"]) {
        let golds = get(row, &["gold"]).or_else(|| get(row, &["expected"])).and_then(Value::as_object).ok_or("missing gold: {question id: answer}")?;
        for (qid, q) in qs.iter() {
            let (q2, keys) = question_keys(q).map_err(|e| format!("question {qid}: {e}"))?;
            let g = golds.get(qid).ok_or_else(|| format!("no gold answer for question {qid}"))?;
            let gold = gold_index(&text(get(q, &["type"])), &keys, g).map_err(|e| format!("question {qid}: {e}"))?;
            questions.push((qid.clone(), q2, keys, gold));
        }
    } else {
        return Err("a row needs question + expected, or questions + gold".into());
    }
    if questions.is_empty() {
        return Err("no question".into());
    }
    let tier = match get(row, &["tier"]) {
        Some(Value::Str(s)) if !s.is_empty() => s.clone(),
        _ => default_tier.to_owned(),
    };
    Ok(Item { id, tier, family: text(get(row, &["family"])), state, questions })
}

pub fn parse_jsonl(text: &str, default_tier: &str) -> Result<Vec<Item>, Vec<String>> {
    let mut items = Vec::new();
    let mut errors = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for (n, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let parsed = loads(line, true).map_err(|e| e.message).and_then(|v| parse_row(&v, n + 1, default_tier));
        match parsed {
            Ok(item) if !seen.insert(item.id.clone()) => errors.push(format!("line {}: duplicate id {}", n + 1, item.id)),
            Ok(item) => items.push(item),
            Err(e) => errors.push(format!("line {}: {e}", n + 1)),
        }
        if errors.len() >= 10 {
            break;
        }
    }
    if items.len() > MAX_ITEMS {
        errors.push(format!("{} items; the limit is {MAX_ITEMS}", items.len()));
    }
    if items.is_empty() && errors.is_empty() {
        errors.push("the file has no rows".into());
    }
    if errors.is_empty() { Ok(items) } else { Err(errors) }
}

/// The embedded public JevBench items of the chosen tiers, in the 1.x order (original, easy, hard).
pub fn jevbench(tiers: &[String]) -> Vec<Item> {
    JEVBENCH
        .iter()
        .filter(|(t, _)| tiers.is_empty() || tiers.iter().any(|x| x == t))
        .flat_map(|(t, text)| parse_jsonl(text, t).expect("embedded JevBench parses"))
        .collect()
}

// ------------------------------------------------------------------------------------------------------- metrics

fn mean(v: &[f64]) -> Option<f64> { if v.is_empty() { None } else { Some(v.iter().sum::<f64>() / v.len() as f64) } }
fn f(v: Option<f64>) -> Value { v.filter(|x| x.is_finite()).map(Value::Float).unwrap_or(Value::Null) }
fn quantile(sorted: &[f64], q: f64) -> Option<f64> {
    if sorted.is_empty() { None } else { Some(sorted[((q * sorted.len() as f64) as usize).min(sorted.len() - 1)]) }
}

/// Per item latency (every question of an item shares it), sorted.
fn latencies(rows: &[&Row]) -> Vec<f64> {
    let mut seen = HashMap::new();
    for r in rows.iter().filter(|r| r.dist.is_some()) {
        seen.entry(r.id.as_str()).or_insert(r.latency_s);
    }
    let mut v: Vec<f64> = seen.into_values().collect();
    v.sort_by(f64::total_cmp);
    v
}

fn chance(rows: &[&Row]) -> f64 { rows.iter().map(|r| 1.0 / r.keys.len() as f64).sum::<f64>() / rows.len().max(1) as f64 }

pub fn group(rows: &[&Row]) -> Value {
    let n = rows.len();
    let valid: Vec<&&Row> = rows.iter().filter(|r| r.dist.is_some()).collect();
    let correct = rows.iter().filter(|r| r.correct()).count();
    let (mut brier, mut nll, mut ordinal) = (Vec::new(), Vec::new(), Vec::new());
    let mut bins: Vec<Vec<(f64, bool)>> = vec![Vec::new(); 10];
    for r in &valid {
        let d = r.dist.as_ref().unwrap();
        brier.push(d.iter().enumerate().map(|(i, p)| (p - if i == r.gold { 1.0 } else { 0.0 }).powi(2)).sum());
        nll.push(-d[r.gold].max(1e-300).ln());
        let best = r.predicted().unwrap();
        bins[((d[best] * 10.0) as usize).min(9)].push((d[best], best == r.gold));
        if r.kind == "score" {
            ordinal.push((d.iter().enumerate().map(|(i, p)| i as f64 * p).sum::<f64>() - r.gold as f64).abs());
        }
    }
    let ece = if valid.is_empty() {
        None
    } else {
        Some(
            bins.iter()
                .filter(|b| !b.is_empty())
                .map(|b| b.len() as f64 / valid.len() as f64 * (b.iter().map(|x| x.0).sum::<f64>() / b.len() as f64 - b.iter().filter(|x| x.1).count() as f64 / b.len() as f64).abs())
                .sum(),
        )
    };
    let acc = if n > 0 { Some(correct as f64 / n as f64) } else { None };
    let ch = chance(rows);
    let lat = latencies(rows);
    let mut m = Map::new();
    m.push_unique("questions", Value::int(n as i64));
    m.push_unique("valid", Value::int(valid.len() as i64));
    m.push_unique("correct", Value::int(correct as i64));
    m.push_unique("accuracy", f(acc));
    m.push_unique("accuracy_on_valid", f(if valid.is_empty() { None } else { Some(correct as f64 / valid.len() as f64) }));
    m.push_unique("chance", f(if n > 0 { Some(ch) } else { None }));
    m.push_unique("skill", f(acc.map(|a| (a - ch) / (1.0 - ch))));
    m.push_unique("brier", f(mean(&brier)));
    m.push_unique("nll", f(mean(&nll)));
    m.push_unique("ece_10", f(ece));
    m.push_unique("ordinal_mae", f(mean(&ordinal)));
    m.push_unique("latency_p50_s", f(quantile(&lat, 0.5)));
    m.push_unique("latency_p95_s", f(quantile(&lat, 0.95)));
    Value::Object(m)
}

fn by<F: Fn(&Row) -> String>(rows: &[Row], key: F) -> Value {
    let mut order: Vec<String> = Vec::new();
    let mut groups: HashMap<String, Vec<&Row>> = HashMap::new();
    for r in rows {
        let k = key(r);
        if !groups.contains_key(&k) {
            order.push(k.clone());
        }
        groups.entry(k).or_default().push(r);
    }
    let mut m = Map::new();
    for k in order {
        m.push_unique(k.clone(), group(&groups[&k]));
    }
    Value::Object(m)
}

/// The Decision Index and its parts.
pub fn decision_index(rows: &[Row]) -> Value {
    let mut tiers: Vec<String> = Vec::new();
    for r in rows {
        if !tiers.contains(&r.tier) {
            tiers.push(r.tier.clone());
        }
    }
    let (mut total, mut weights) = (0.0, 0.0);
    let mut parts = Vec::new();
    for t in &tiers {
        let rs: Vec<&Row> = rows.iter().filter(|r| &r.tier == t).collect();
        let acc = rs.iter().filter(|r| r.correct()).count() as f64 / rs.len() as f64;
        let ch = chance(&rs);
        let skill = ((acc - ch) / (1.0 - ch)).max(0.0);
        let w = TIER_WEIGHTS.iter().find(|(n, _)| n == t).map(|(_, w)| *w).unwrap_or(if tiers.len() == 1 { 1.0 } else { 28.0 });
        total += w * skill;
        weights += w;
        let mut p = Map::new();
        p.push_unique("tier", Value::Str(t.clone()));
        p.push_unique("weight", Value::Float(w));
        p.push_unique("questions", Value::int(rs.len() as i64));
        p.push_unique("accuracy", Value::Float(acc));
        p.push_unique("chance", Value::Float(ch));
        p.push_unique("skill", Value::Float(skill));
        parts.push(Value::Object(p));
    }
    let lat: Vec<&Row> = rows.iter().collect();
    let lat = latencies(&lat);
    let speed = |s: f64| (100.0 - 20.0 * (s.max(1e-6) / 0.1).log10()).clamp(0.0, 100.0);
    let mut m = Map::new();
    m.push_unique("value", f(if weights > 0.0 { Some(100.0 * total / weights) } else { None }));
    m.push_unique("tiers", Value::Array(parts));
    m.push_unique(
        "speed",
        f(match (quantile(&lat, 0.5), quantile(&lat, 0.95)) {
            (Some(a), Some(b)) => Some((speed(a) + speed(b)) / 2.0),
            _ => None,
        }),
    );
    m.push_unique(
        "formula",
        Value::str("100 x sum(w_t x max(0, (accuracy_t - chance_t) / (1 - chance_t))) / sum(w_t); w: hard 30, original (standard) 28, easy 14; failures count as wrong"),
    );
    m.push_unique("speed_formula", Value::str("mean of s(p50) and s(p95), s(t) = 100 - 20 log10(t / 0.1 s), clipped to 0-100 (JevBench), latency of this computer"));
    m.push_unique("note", Value::str("JevBench Intelligence axis on the public items only; not the JevBench leaderboard score"));
    Value::Object(m)
}

fn rate(k: usize, n: usize) -> Value { if n == 0 { Value::Null } else { Value::Float(k as f64 / n as f64) } }

pub fn probes(rows: &[Row]) -> Value {
    let checked: Vec<&Row> = rows.iter().filter(|r| r.flip.is_some()).collect();
    let flips = checked.iter().filter(|r| r.flip == Some(true)).count();
    let shifts: Vec<f64> = checked.iter().filter_map(|r| r.shift).collect();
    let mut order = Map::new();
    order.push_unique("checked", Value::int(checked.len() as i64));
    order.push_unique("top_choice_changed", Value::int(flips as i64));
    order.push_unique("flip_rate", rate(flips, checked.len()));
    order.push_unique("mean_max_shift", f(mean(&shifts)));
    order.push_unique("max_shift", f(shifts.iter().cloned().reduce(f64::max)));

    let choice: Vec<&Row> = rows.iter().filter(|r| r.kind == "choice" && r.keys.len() > 2).collect();
    let valid: Vec<&&Row> = choice.iter().filter(|r| r.dist.is_some()).collect();
    let pf = valid.iter().filter(|r| r.predicted() == Some(0)).count();
    let gf = valid.iter().filter(|r| r.gold == 0).count();
    let pl = valid.iter().filter(|r| r.predicted() == Some(r.keys.len() - 1)).count();
    let gl = valid.iter().filter(|r| r.gold == r.keys.len() - 1).count();
    let mut position = Map::new();
    position.push_unique("questions", Value::int(valid.len() as i64));
    position.push_unique("predicted_first", rate(pf, valid.len()));
    position.push_unique("gold_first", rate(gf, valid.len()));
    position.push_unique("predicted_last", rate(pl, valid.len()));
    position.push_unique("gold_last", rate(gl, valid.len()));
    position.push_unique("first_excess", f(if valid.is_empty() { None } else { Some((pf as f64 - gf as f64) / valid.len() as f64) }));
    position.push_unique("last_excess", f(if valid.is_empty() { None } else { Some((pl as f64 - gl as f64) / valid.len() as f64) }));

    let noul: Vec<&Row> = rows.iter().filter(|r| r.kind == "noul" && r.dist.is_some()).collect();
    let pt = noul.iter().filter(|r| r.predicted() == Some(1)).count();
    let gt = noul.iter().filter(|r| r.gold == 1).count();
    let mut polarity = Map::new();
    polarity.push_unique("questions", Value::int(noul.len() as i64));
    polarity.push_unique("predicted_yes", rate(pt, noul.len()));
    polarity.push_unique("gold_yes", rate(gt, noul.len()));
    polarity.push_unique("yes_excess", f(if noul.is_empty() { None } else { Some((pt as f64 - gt as f64) / noul.len() as f64) }));

    let score: Vec<&Row> = rows.iter().filter(|r| r.kind == "score" && r.dist.is_some()).collect();
    let expected: Vec<f64> = score.iter().map(|r| r.dist.as_ref().unwrap().iter().enumerate().map(|(i, p)| i as f64 * p).sum()).collect();
    let golds: Vec<f64> = score.iter().map(|r| r.gold as f64).collect();
    let mut leniency = Map::new();
    leniency.push_unique("questions", Value::int(score.len() as i64));
    leniency.push_unique("mean_expected_level", f(mean(&expected)));
    leniency.push_unique("mean_gold_level", f(mean(&golds)));
    leniency.push_unique("excess", f(mean(&expected).zip(mean(&golds)).map(|(a, b)| a - b)));

    let mut m = Map::new();
    m.push_unique("order", Value::Object(order));
    m.push_unique("position", Value::Object(position));
    m.push_unique("polarity", Value::Object(polarity));
    m.push_unique("leniency", Value::Object(leniency));
    m.push_unique("note", Value::str("order: the two-permutation diagnostic of each Choice answer; position, polarity and leniency compare the answers with the gold answers of the same questions"));
    Value::Object(m)
}

/// Everything shown on the Evaluate page.
pub fn summary(rows: &[Row]) -> Value {
    let all: Vec<&Row> = rows.iter().collect();
    let mut selective = Map::new();
    for t in [0.5, 0.7, 0.85, 0.9, 0.95] {
        let accepted: Vec<&Row> = rows.iter().filter(|r| r.predicted().is_some_and(|b| r.dist.as_ref().unwrap()[b] >= t)).collect();
        let ok = accepted.iter().filter(|r| r.correct()).count();
        let mut s = Map::new();
        s.push_unique("accepted", Value::int(accepted.len() as i64));
        s.push_unique("coverage", rate(accepted.len(), rows.len()));
        s.push_unique("accuracy", rate(ok, accepted.len()));
        selective.push_unique(format!("{t}"), Value::Object(s));
    }
    let mut m = Map::new();
    m.push_unique("decision_index", decision_index(rows));
    m.push_unique("overall", group(&all));
    m.push_unique("by_tier", by(rows, |r| r.tier.clone()));
    m.push_unique("by_type", by(rows, |r| r.kind.clone()));
    m.push_unique("by_engine", by(rows, |r| match (r.dist.is_some(), r.engine.is_empty()) {
        (false, _) => "failed".into(),
        (true, true) => "unknown".into(),
        (true, false) => r.engine.clone(),
    }));
    m.push_unique("by_family", by(rows, |r| if r.family.is_empty() { "-".into() } else { r.family.clone() }));
    m.push_unique("selective", Value::Object(selective));
    m.push_unique("probes", probes(rows));
    Value::Object(m)
}

// ------------------------------------------------------------------------------------------------------- the job

pub struct Config {
    pub source: String,
    pub engine: Option<Engine>,
    pub combine: bool,
    pub probes: bool,
}

pub struct Job {
    pub id: String,
    pub created: String,
    pub config: Value,
    pub total: usize,
    pub done: usize,
    pub rows: Vec<Row>,
    pub status: &'static str,
    pub error: Option<String>,
    pub saved: Option<String>,
    pub cancel: Arc<AtomicBool>,
    began: Instant,
}

impl Job {
    pub fn to_value(&self, with_summary: bool) -> Value {
        let mut m = Map::new();
        m.push_unique("id", Value::Str(self.id.clone()));
        m.push_unique("status", Value::str(self.status));
        m.push_unique("created", Value::Str(self.created.clone()));
        m.push_unique("config", self.config.clone());
        m.push_unique("done", Value::int(self.done as i64));
        m.push_unique("total", Value::int(self.total as i64));
        m.push_unique("elapsed_s", Value::Float(self.began.elapsed().as_secs_f64()));
        m.push_unique("error", self.error.clone().map(Value::Str).unwrap_or(Value::Null));
        m.push_unique("saved", self.saved.clone().map(Value::Str).unwrap_or(Value::Null));
        if with_summary {
            m.push_unique("summary", if self.rows.is_empty() { Value::Null } else { summary(&self.rows) });
        }
        Value::Object(m)
    }
}

pub fn folder(data: &Path) -> PathBuf { data.join("evaluations") }

fn answer_rows(item: &Item, reply: &Value, latency: f64) -> Vec<Row> {
    item.questions
        .iter()
        .map(|(qid, q, keys, gold)| {
            let a = get(reply, &["answers", qid]);
            let dist: Option<Vec<f64>> = a.and_then(|a| keys.iter().map(|k| num(get(a, &["probabilities", k]))).collect());
            let diag = a.and_then(|a| get(a, &["order_diagnostic"])).filter(|d| matches!(get(d, &["checked"]), Some(Value::Bool(true))));
            Row {
                id: item.id.clone(),
                tier: item.tier.clone(),
                family: item.family.clone(),
                qid: qid.clone(),
                kind: text(get(q, &["type"])),
                keys: keys.clone(),
                gold: *gold,
                error: if dist.is_none() { Some("the answer has no distribution over the options".into()) } else { None },
                dist,
                engine: text(a.and_then(|a| get(a, &["engine"]))),
                latency_s: latency,
                flip: diag.and_then(|d| match get(d, &["top_choice_changed"]) {
                    Some(Value::Bool(b)) => Some(*b),
                    _ => None,
                }),
                shift: diag.and_then(|d| num(get(d, &["max_probability_shift"]))),
            }
        })
        .collect()
}

fn failed_rows(item: &Item, error: &str, latency: f64) -> Vec<Row> {
    item.questions
        .iter()
        .map(|(qid, q, keys, gold)| Row {
            id: item.id.clone(),
            tier: item.tier.clone(),
            family: item.family.clone(),
            qid: qid.clone(),
            kind: text(get(q, &["type"])),
            keys: keys.clone(),
            gold: *gold,
            dist: None,
            engine: String::new(),
            latency_s: latency,
            error: Some(error.chars().take(300).collect()),
            flip: None,
            shift: None,
        })
        .collect()
}

pub fn new_job(config: &Config, total: usize) -> Job {
    let mut c = Map::new();
    c.push_unique("source", Value::Str(config.source.clone()));
    c.push_unique("engine", Value::str(config.engine.map(Engine::name).unwrap_or("auto")));
    c.push_unique("combine", Value::str(if config.combine { "ensemble" } else { "route" }));
    c.push_unique("probes", Value::Bool(config.probes));
    if config.source == "jevbench" {
        c.push_unique("dataset", Value::str(DATASET));
    }
    Job {
        id: random_hex(8),
        created: now_iso(),
        config: Value::Object(c),
        total,
        done: 0,
        rows: Vec::new(),
        status: "running",
        error: None,
        saved: None,
        cancel: Arc::new(AtomicBool::new(false)),
        began: Instant::now(),
    }
}

/// Answer every item (in this thread), updating the shared job; then save `data/evaluations/<id>.json`.
pub fn run(job: &Mutex<Job>, items: Vec<Item>, engines: Arc<dyn Decide>, config: Config, data: &Path) {
    let cancel = job.lock().unwrap_or_else(|e| e.into_inner()).cancel.clone();
    for item in &items {
        if cancel.load(Ordering::Relaxed) {
            break;
        }
        let mut qs = Map::new();
        for (qid, q, _, _) in &item.questions {
            qs.push_unique(qid.clone(), q.clone());
        }
        let mut payload = Map::new();
        payload.push_unique("state", Value::Str(item.state.clone()));
        payload.push_unique("questions", Value::Object(qs));
        payload.push_unique("permutation_check", Value::Bool(config.probes));
        let began = Instant::now();
        let reply = engines.evaluate(&Value::Object(payload), config.engine, config.combine);
        let latency = began.elapsed().as_secs_f64();
        let rows = match reply {
            Ok(r) => answer_rows(item, &r, latency),
            Err(e) => failed_rows(item, &e.message, latency),
        };
        let mut j = job.lock().unwrap_or_else(|e| e.into_inner());
        j.rows.extend(rows);
        j.done += 1;
    }
    let mut j = job.lock().unwrap_or_else(|e| e.into_inner());
    j.status = if cancel.load(Ordering::Relaxed) { "stopped" } else { "done" };
    if j.rows.is_empty() {
        return;
    }
    let mut record = Map::new();
    record.push_unique("format", Value::str(FORMAT));
    record.push_unique("id", Value::Str(j.id.clone()));
    record.push_unique("created", Value::Str(j.created.clone()));
    record.push_unique("status", Value::str(j.status));
    record.push_unique("config", j.config.clone());
    record.push_unique("items", Value::int(j.done as i64));
    record.push_unique("summary", summary(&j.rows));
    record.push_unique("rows", Value::Array(j.rows.iter().map(Row::to_value).collect()));
    let name = format!("{}-{}.json", j.created.get(..19).unwrap_or("").replace([':', '-'], "").replace('T', "-"), j.id);
    let path = folder(data).join(&name);
    match dumps_indent(&Value::Object(record), 1, DumpOptions::REPLY).map_err(|e| e.message).and_then(|t| write_atomic(&path, t.as_bytes()).map_err(|e| e.to_string())) {
        Ok(()) => j.saved = Some(name),
        Err(e) => j.error = Some(format!("results not saved: {e}")),
    }
}

fn valid_name(name: &str) -> bool {
    name.ends_with(".json") && name.len() < 80 && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '.') && !name.starts_with('.')
}

/// Saved evaluations, newest first: file name, date, configuration and the headline numbers.
pub fn list(data: &Path) -> Value {
    let mut out = Vec::new();
    if let Ok(dir) = std::fs::read_dir(folder(data)) {
        let mut names: Vec<String> = dir.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).filter(|n| valid_name(n)).collect();
        names.sort();
        names.reverse();
        for name in names.into_iter().take(200) {
            let Ok(text) = std::fs::read_to_string(folder(data).join(&name)) else { continue };
            let Ok(v) = loads(&text, true) else { continue };
            if text_of(&v, "format") != FORMAT {
                continue;
            }
            let mut m = Map::new();
            m.push_unique("name", Value::Str(name));
            m.push_unique("created", get(&v, &["created"]).cloned().unwrap_or(Value::Null));
            m.push_unique("status", get(&v, &["status"]).cloned().unwrap_or(Value::Null));
            m.push_unique("config", get(&v, &["config"]).cloned().unwrap_or(Value::Null));
            m.push_unique("decision_index", get(&v, &["summary", "decision_index", "value"]).cloned().unwrap_or(Value::Null));
            m.push_unique("accuracy", get(&v, &["summary", "overall", "accuracy"]).cloned().unwrap_or(Value::Null));
            m.push_unique("brier", get(&v, &["summary", "overall", "brier"]).cloned().unwrap_or(Value::Null));
            m.push_unique("questions", get(&v, &["summary", "overall", "questions"]).cloned().unwrap_or(Value::Null));
            out.push(Value::Object(m));
        }
    }
    let mut m = Map::new();
    m.push_unique("evaluations", Value::Array(out));
    Value::Object(m)
}

fn text_of(v: &Value, key: &str) -> String { text(get(v, &[key])) }

/// One saved evaluation: the whole record, or its rows as JSONL.
pub fn read(data: &Path, name: &str, jsonl: bool) -> Result<(String, &'static str), String> {
    if !valid_name(name) {
        return Err("invalid evaluation name".into());
    }
    let text = std::fs::read_to_string(folder(data).join(name)).map_err(|_| "evaluation not found".to_owned())?;
    if !jsonl {
        return Ok((text, "application/json; charset=utf-8"));
    }
    let v = loads(&text, true).map_err(|e| e.message)?;
    let mut out = String::new();
    for r in get(&v, &["rows"]).and_then(Value::as_array).cloned().unwrap_or_default() {
        out.push_str(&dumps(&r, DumpOptions::REPLY).map_err(|e| e.message)?.replace('\n', ""));
        out.push('\n');
    }
    Ok((out, "application/x-ndjson; charset=utf-8"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(tier: &str, kind: &str, k: usize, gold: usize, dist: Option<Vec<f64>>) -> Row {
        Row {
            id: format!("{tier}-{gold}-{:?}", dist),
            tier: tier.into(),
            family: String::new(),
            qid: "q".into(),
            kind: kind.into(),
            keys: (0..k).map(|i| i.to_string()).collect(),
            gold,
            dist,
            engine: "generalist".into(),
            latency_s: 0.1,
            error: None,
            flip: None,
            shift: None,
        }
    }

    #[test]
    fn embedded_jevbench_has_231_items() {
        let items = jevbench(&[]);
        assert_eq!(items.len(), 231);
        let count = |t: &str| items.iter().filter(|i| i.tier == t).count();
        assert_eq!((count("original"), count("easy"), count("hard")), (72, 48, 111));
        // canonical keys of a Choice question follow the criteria order; gold indexes are inside the options
        assert!(items.iter().all(|i| i.questions.len() == 1 && i.questions[0].3 < i.questions[0].2.len()));
        assert_eq!(items.iter().filter(|i| i.questions[0].2 == ["false", "true"]).count(), 24 + 12 + 38);
        assert_eq!(jevbench(&["easy".into()]).len(), 48);
    }

    #[test]
    fn metrics_follow_jevbench() {
        let rows = vec![
            row("hard", "noul", 2, 1, Some(vec![0.2, 0.8])),
            row("hard", "noul", 2, 0, Some(vec![0.3, 0.7])),
            row("hard", "choice", 4, 2, None),
            row("easy", "score", 4, 3, Some(vec![0.0, 0.0, 0.5, 0.5])),
        ];
        let all: Vec<&Row> = rows.iter().collect();
        let g = group(&all);
        assert_eq!(get(&g, &["correct"]), Some(&Value::int(1)));
        assert_eq!(num(get(&g, &["accuracy"])), Some(0.25));
        // chance = mean(1/2, 1/2, 1/4, 1/4) = 0.375
        assert!((num(get(&g, &["chance"])).unwrap() - 0.375).abs() < 1e-12);
        // Brier over valid answers: 0.08, 0.98, 0.5
        assert!((num(get(&g, &["brier"])).unwrap() - (0.08 + 0.98 + 0.5) / 3.0).abs() < 1e-12);
        assert!((num(get(&g, &["ordinal_mae"])).unwrap() - 0.5).abs() < 1e-12);
        // ECE: bins 0.8 (1 right), 0.7 (wrong), 0.5 (argmax index 2, wrong) -> (0.2 + 0.7 + 0.5) / 3
        assert!((num(get(&g, &["ece_10"])).unwrap() - (0.2 + 0.7 + 0.5) / 3.0).abs() < 1e-12);
        // index: hard acc 1/3, chance 5/12 -> skill 0; easy acc 0 -> 0
        assert_eq!(num(get(&decision_index(&rows), &["value"])), Some(0.0));
        let good = vec![row("hard", "noul", 2, 1, Some(vec![0.1, 0.9])), row("easy", "choice", 4, 0, Some(vec![0.7, 0.1, 0.1, 0.1]))];
        assert!((num(get(&decision_index(&good), &["value"])).unwrap() - 100.0).abs() < 1e-9);
        let p = probes(&good);
        assert_eq!(num(get(&p, &["position", "predicted_first"])), Some(1.0));
        assert_eq!(num(get(&p, &["polarity", "yes_excess"])), Some(0.0));
    }

    #[test]
    fn user_rows_in_both_formats() {
        let text = concat!(
            r#"{"id": "a", "state": "x", "question": {"type": "noul", "instructions": "Is it?"}, "expected": "yes"}"#,
            "\n",
            r#"{"id": "b", "case": {"k": 1}, "questions": {"r": {"type": "choice", "instructions": "Which?", "criteria": ["p", "q"]}}, "gold": {"r": "q"}}"#,
            "\n",
        );
        let items = parse_jsonl(text, "file").unwrap();
        assert_eq!(items[0].questions[0].3, 1);
        assert_eq!(items[1].state, "{\n \"k\": 1\n}");
        assert_eq!(items[1].questions[0].3, 1);
        assert_eq!(dumps(&items[1].questions[0].1, DumpOptions::REPLY).unwrap().replace(['\n', ' '], ""), r#"{"type":"choice","instructions":"Which?","criteria":{"p":null,"q":null}}"#);
        let bad = parse_jsonl("{\"id\": \"a\", \"state\": \"x\", \"question\": {\"type\": \"score\", \"criteria\": [\"a\", \"b\"]}, \"expected\": 5}\n", "file").unwrap_err();
        assert!(bad[0].contains("line 1") && bad[0].contains("not an option"), "{bad:?}");
    }
}
