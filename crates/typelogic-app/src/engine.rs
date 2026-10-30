//! Decisions over the models of the llama.cpp router, with the rules of TypeLogic 1.x (`typelogic/hybrid.py`):
//!
//!   * a question with at most 16 options -> the generalist (Plumb, direct letter readout, two option orders)
//!   * a larger one                       -> Clef-Flash when it is loaded, otherwise the specialist
//!   * with no generalist loaded, every question goes to that decision model.
//!
//! Answers are built by typelogic-core (`schema.answers`, the 1.x rules and review reasons) and keep the request
//! order; each records the engine that produced it. Choice questions get the two-permutation order diagnostic of
//! 1.x unless `permutation_check` is false.
use crate::direct;
use crate::http;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::OnceLock;
use std::time::{Duration, Instant};
use typelogic_core::json::{dumps, loads, DumpOptions, Map, Value};
use typelogic_core::schema::{self, Question};

const TIMEOUT: Duration = Duration::from_secs(900);
/// Shared prompt prefix worth evaluating once before the questions of a request (tokens).
const MIN_WARM_PREFIX: usize = 64;
/// Calibration labels of 1.x: fitted on local data (specialist, Plumb), or none (Clef-Flash).
const VALIDATED: &str = "local_validation_not_general_guarantee";
/// Weight of the generalist in the combined mode, `p ~ generalist^w * other^(1-w)`: 0.55 with the specialist (1.x,
/// chosen on typelogic-distill-v1), 0.5 with Clef-Flash (docs/SPIKE-RESULTS.md, combinations).
pub const WEIGHT_WITH_SPECIALIST: f64 = 0.55;
pub const WEIGHT_WITH_CLEF: f64 = 0.5;
/// Review reasons recomputed by `schema.answers` for a combined answer (the others are carried over).
const RECOMPUTED_REASONS: [&str; 4] = ["human_review_required", "below_threshold", "abstention_not_declared", "explicit_abstention"];
const NOT_VALIDATED: &str = "not_validated_for_your_domain";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Engine {
    Generalist,
    Specialist,
    Clef,
}

impl Engine {
    /// The model name in the router presets (layout::ROLES).
    pub fn name(self) -> &'static str {
        match self {
            Engine::Generalist => "generalist",
            Engine::Specialist => "specialist",
            Engine::Clef => "clef",
        }
    }

    pub fn parse(s: &str) -> Option<Engine> {
        match s {
            "generalist" | "plumb" => Some(Engine::Generalist),
            "specialist" => Some(Engine::Specialist),
            "clef" => Some(Engine::Clef),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ApiError {
    pub status: u16,
    pub message: String,
}

fn bad(message: impl Into<String>) -> ApiError { ApiError { status: 400, message: message.into() } }
fn upstream(message: impl Into<String>) -> ApiError { ApiError { status: 502, message: message.into() } }

pub struct Engines {
    pub addr: SocketAddr,
    /// Models loaded by the router.
    pub loaded: Vec<Engine>,
    /// Context of the generalist instance (tokens); a longer prompt is refused, never truncated.
    pub generalist_ctx: usize,
    slots: OnceLock<Vec<i64>>,
}

/// Engine for one question, or why none can answer it.
pub fn route(q: &Question, loaded: &[Engine], force: Option<Engine>) -> Result<Engine, String> {
    let n = direct::option_count(q);
    let catalog = [Engine::Clef, Engine::Specialist].into_iter().find(|e| loaded.contains(e));
    if let Some(e) = force {
        if !loaded.contains(&e) {
            return Err(format!("the {} model is not loaded", e.name()));
        }
        if e == Engine::Generalist && n > direct::MAX_OPTIONS {
            return Err(format!("{}: the generalist reads at most {} options", q.id, direct::MAX_OPTIONS));
        }
        return Ok(e);
    }
    if n <= direct::MAX_OPTIONS && loaded.contains(&Engine::Generalist) {
        return Ok(Engine::Generalist);
    }
    catalog.ok_or_else(|| format!("{}: questions with more than {} options need the specialist or Clef-Flash", q.id, direct::MAX_OPTIONS))
}

fn obj(v: &Value) -> Option<&Map> { v.as_object() }
fn get<'a>(v: &'a Value, path: &[&str]) -> Option<&'a Value> {
    let mut cur = v;
    for k in path {
        cur = obj(cur)?.get(k)?;
    }
    Some(cur)
}
fn num(v: Option<&Value>) -> Option<f64> { v.and_then(|x| x.to_f64().ok()) }

/// Distribution over the canonical keys of a question from a /v1/systemone answer.
pub fn systemone_row(q: &Question, answer: &Value) -> Result<Vec<f64>, String> {
    if q.kind == "noul" {
        let p = num(get(answer, &["noul"])).ok_or("answer without noul")?;
        return Ok(vec![1.0 - p, p]);
    }
    q.keys.iter().map(|k| num(get(answer, &["probabilities", k])).ok_or_else(|| format!("answer without probability for {k}"))).collect()
}

fn probabilities_of(keys: &[String], row: &[f64]) -> HashMap<String, f64> { keys.iter().cloned().zip(row.iter().copied()).collect() }

/// The 1.x order diagnostic from the canonical reading and the permuted readings (probabilities keyed by option key).
fn order_diagnostic(original: &HashMap<String, f64>, variants: &[HashMap<String, f64>], chosen: &str, averaged: Option<usize>) -> (Value, bool) {
    let argmax = |p: &HashMap<String, f64>| p.iter().fold(None::<(&String, f64)>, |b, (k, v)| if b.is_none_or(|(_, bv)| *v > bv) { Some((k, *v)) } else { b }).map(|(k, _)| k.clone());
    let mut shift = 0f64;
    for v in variants {
        for (k, p) in original {
            shift = shift.max((p - v.get(k).copied().unwrap_or(0.0)).abs());
        }
    }
    let flipped = std::iter::once(original).chain(variants).any(|p| argmax(p).as_deref() != Some(chosen));
    let unstable = flipped || shift > 0.15;
    let mut m = Map::new();
    m.push_unique("checked", Value::Bool(true));
    m.push_unique("permutations", Value::int(2));
    m.push_unique("max_probability_shift", Value::Float(shift));
    m.push_unique("top_choice_changed", Value::Bool(flipped));
    m.push_unique("unstable", Value::Bool(unstable));
    if let Some(n) = averaged {
        m.push_unique("averaged_orders", Value::int(n as i64));
    }
    m.push_unique("scope", Value::str("two permutations; not a robustness certificate"));
    (Value::Object(m), unstable)
}

/// Append a review reason; `review` also turns the recommendation to review (as 1.x does for diagnostics).
fn add_reason(answer: &mut Map, reason: &str, review: bool) {
    let mut reasons = answer.get("review_reasons").and_then(Value::as_array).cloned().unwrap_or_default();
    reasons.push(Value::str(reason));
    answer.insert("review_reasons", Value::Array(reasons));
    if review {
        answer.insert("recommendation", Value::str("review"));
    }
}

/// Probabilities keyed by option key.
type Distribution = HashMap<String, f64>;
/// One reading of the generalist: (question index, option order, prompt tokens, option ids in prompt order).
type Reading = (usize, Vec<usize>, Vec<i64>, Vec<String>);

/// Weighted geometric mean of two distributions over the same keys, renormalised (`hybrid.product_of_experts`).
pub fn product_of_experts(generalist: &[f64], other: &[f64], weight: f64) -> Vec<f64> {
    let logs: Vec<f64> = generalist.iter().zip(other).map(|(g, o)| weight * g.max(1e-12).ln() + (1.0 - weight) * o.max(1e-12).ln()).collect();
    let top = logs.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let total: f64 = logs.iter().map(|v| (v - top).exp()).sum();
    logs.iter().map(|v| (v - top).exp() / total).collect()
}

fn probs_of(answer: &Value, keys: &[String]) -> Option<Vec<f64>> {
    keys.iter().map(|k| num(get(answer, &["probabilities", k]))).collect()
}

/// Answers of one engine for its questions: (answers by question id, input tokens).
struct Part {
    answers: Map,
    tokens: i64,
}

impl Engines {
    pub fn new(addr: SocketAddr, loaded: Vec<Engine>, generalist_ctx: usize) -> Self { Self { addr, loaded, generalist_ctx, slots: OnceLock::new() } }

    fn post(&self, path: &str, body: &Value) -> Result<Value, ApiError> {
        let text = dumps(body, DumpOptions::REPLY).map_err(|e| upstream(e.message))?;
        let (status, data) = http::call(self.addr, "POST", path, Some(text.as_bytes()), TIMEOUT).map_err(|e| upstream(format!("inference server: {e}")))?;
        let reply = loads(&String::from_utf8_lossy(&data), false).unwrap_or(Value::Null);
        if status != 200 {
            let message = get(&reply, &["error", "message"]).and_then(Value::as_str).map(str::to_owned).unwrap_or_else(|| String::from_utf8_lossy(&data).chars().take(300).collect());
            // a request the model cannot read (too long, too many options) is the caller's to fix
            return Err(ApiError { status: if status == 400 { 400 } else { 502 }, message: format!("inference server ({status}): {message}") });
        }
        Ok(reply)
    }

    fn tokenize(&self, text: &str) -> Result<Vec<i64>, ApiError> {
        let mut body = Map::new();
        body.push_unique("model", Value::str(Engine::Generalist.name()));
        body.push_unique("content", Value::str(text));
        body.push_unique("add_special", Value::Bool(false));
        body.push_unique("parse_special", Value::Bool(true));
        let reply = self.post("/tokenize", &Value::Object(body))?;
        get(&reply, &["tokens"])
            .and_then(Value::as_array)
            .ok_or_else(|| upstream("tokenize: no tokens"))?
            .iter()
            .map(|t| num(Some(t)).map(|x| x as i64).ok_or_else(|| upstream("tokenize: bad token")))
            .collect()
    }

    /// One chat completion of the generalist with JSON output constrained to `schema`; the message content.
    pub fn chat_json(&self, system: &str, user: &str, schema: &Value, max_tokens: u32) -> Result<String, ApiError> {
        if !self.loaded.contains(&Engine::Generalist) {
            return Err(ApiError { status: 503, message: "the generalist model (Plumb) is not loaded".into() });
        }
        let message = |role: &str, content: &str| {
            let mut m = Map::new();
            m.push_unique("role", Value::str(role));
            m.push_unique("content", Value::str(content));
            Value::Object(m)
        };
        let mut format = Map::new();
        format.push_unique("type", Value::str("json_schema"));
        let mut js = Map::new();
        js.push_unique("name", Value::str("answer"));
        js.push_unique("schema", schema.clone());
        format.push_unique("json_schema", Value::Object(js));
        let mut kwargs = Map::new();
        kwargs.push_unique("enable_thinking", Value::Bool(false));
        let mut body = Map::new();
        body.push_unique("model", Value::str(Engine::Generalist.name()));
        body.push_unique("messages", Value::Array(vec![message("system", system), message("user", user)]));
        body.push_unique("temperature", Value::int(0));
        body.push_unique("max_tokens", Value::int(max_tokens as i64));
        body.push_unique("response_format", Value::Object(format));
        body.push_unique("chat_template_kwargs", Value::Object(kwargs));
        let reply = self.post("/v1/chat/completions", &Value::Object(body))?;
        let choice = get(&reply, &["choices"]).and_then(Value::as_array).and_then(|a| a.first()).ok_or_else(|| upstream("chat: no choice"))?;
        if get(choice, &["finish_reason"]).and_then(Value::as_str) == Some("length") && max_tokens > 64 {
            return Err(upstream("the answer did not fit; extract from a shorter part"));
        }
        get(choice, &["message", "content"]).and_then(Value::as_str).map(str::to_owned).ok_or_else(|| upstream("chat: no content"))
    }

    /// Tokens of a text for the generalist (to refuse a prompt that does not fit, instead of cutting it).
    pub fn count_tokens(&self, text: &str) -> Result<usize, ApiError> { self.tokenize(text).map(|t| t.len()) }

    /// Token ids of the answer letters A-P (each must be one token).
    fn slots(&self) -> Result<&Vec<i64>, ApiError> {
        if let Some(s) = self.slots.get() {
            return Ok(s);
        }
        let mut ids = Vec::with_capacity(direct::LETTERS.len());
        for letter in direct::LETTERS {
            match self.tokenize(letter)?.as_slice() {
                [id] => ids.push(*id),
                _ => return Err(upstream(format!("answer slot {letter} is not one token"))),
            }
        }
        Ok(self.slots.get_or_init(|| ids))
    }

    /// Letter log-probabilities of one prompt: (values for the first `count` letters, letter missing from the top 64?).
    fn letter_logits(&self, ids: &[i64], count: usize) -> Result<(Vec<f64>, bool), ApiError> {
        let mut body = Map::new();
        body.push_unique("model", Value::str(Engine::Generalist.name()));
        body.push_unique("prompt", Value::Array(ids.iter().map(|i| Value::int(*i)).collect()));
        body.push_unique("n_predict", Value::int(1));
        body.push_unique("n_probs", Value::int(64));
        body.push_unique("temperature", Value::int(0));
        body.push_unique("cache_prompt", Value::Bool(true));
        body.push_unique("post_sampling_probs", Value::Bool(false));
        let reply = self.post("/completion", &Value::Object(body))?;
        let top = get(&reply, &["completion_probabilities"])
            .and_then(Value::as_array)
            .and_then(|a| a.first())
            .and_then(|p| get(p, &["top_logprobs"]))
            .and_then(Value::as_array)
            .ok_or_else(|| upstream("completion without log-probabilities"))?;
        let mut by_id: HashMap<i64, f64> = HashMap::new();
        for alt in top {
            if let (Some(id), Some(lp)) = (num(get(alt, &["id"])), num(get(alt, &["logprob"]))) {
                by_id.entry(id as i64).or_insert(lp);
            }
        }
        let slots = &self.slots()?[..count];
        let known: Vec<f64> = slots.iter().filter_map(|s| by_id.get(s).copied()).collect();
        if known.is_empty() {
            return Err(upstream("no answer letter among the top log-probabilities"));
        }
        let floor = by_id.values().cloned().fold(f64::INFINITY, f64::min) - 1000f64.ln();
        Ok((slots.iter().map(|s| by_id.get(s).copied().unwrap_or(floor)).collect(), known.len() < count))
    }

    /// The generalist on its questions: two option orders averaged, plus the rotated reading of the order check.
    fn generalist(&self, state: &str, questions: &[&Question], threshold: f64, check: bool) -> Result<Part, ApiError> {
        const ORDERS: usize = 2;
        // every prompt of the request, tokenized first: the length limit is checked before any reading
        let mut plans: Vec<Reading> = Vec::new();
        for (qi, q) in questions.iter().enumerate() {
            let n = direct::option_count(q);
            let mut orders = direct::view_orders(n, ORDERS);
            if check && q.kind == "choice" {
                orders.push(direct::rotated(n));
            }
            for order in orders {
                let (text, option_ids) = direct::prompt(state, q, &order).map_err(bad)?;
                let ids = self.tokenize(&text)?;
                if ids.len() + 16 > self.generalist_ctx {
                    return Err(bad(format!("{}: {} input tokens exceed the generalist limit of {}; nothing is truncated", q.id, ids.len(), self.generalist_ctx - 16)));
                }
                plans.push((qi, order, ids, option_ids));
            }
        }
        if questions.len() > 1 {
            // the evidence shared by all prompts is read once; llama-server keeps a checkpoint of it for the others
            let all: Vec<Vec<i64>> = plans.iter().map(|p| p.2.clone()).collect();
            let n = direct::common_prefix(&all);
            if n >= MIN_WARM_PREFIX {
                let mut body = Map::new();
                body.push_unique("model", Value::str(Engine::Generalist.name()));
                body.push_unique("prompt", Value::Array(all[0][..n].iter().map(|i| Value::int(*i)).collect()));
                body.push_unique("n_predict", Value::int(0));
                body.push_unique("cache_prompt", Value::Bool(true));
                self.post("/completion", &Value::Object(body))?;
            }
        }
        let mut tokens = 0i64;
        let mut views: Vec<Vec<(Vec<usize>, Distribution)>> = vec![Vec::new(); questions.len()];
        let mut floored = vec![false; questions.len()];
        for (qi, order, ids, option_ids) in &plans {
            let (logits, low) = self.letter_logits(ids, option_ids.len())?;
            tokens += ids.len() as i64;
            floored[*qi] |= low;
            let p = direct::softmax(&logits, 1.0).map_err(upstream)?;
            views[*qi].push((order.clone(), option_ids.iter().cloned().zip(p).collect()));
        }
        let rows: Vec<Vec<f64>> = questions
            .iter()
            .zip(&views)
            .map(|(q, v)| {
                let averaged = &v[..ORDERS.min(v.len())];
                q.keys.iter().map(|k| averaged.iter().map(|(_, p)| p[k]).sum::<f64>() / averaged.len() as f64).collect()
            })
            .collect();
        let owned: Vec<Question> = questions.iter().map(|q| (*q).clone()).collect();
        let mut answers = schema::answers(&owned, &rows, threshold, &Value::str(VALIDATED)).map_err(|e| upstream(e.message))?;
        for (qi, q) in questions.iter().enumerate() {
            let Some(Value::Object(a)) = answers.get(&q.id).cloned() else { continue };
            let mut a = a;
            if floored[qi] {
                add_reason(&mut a, "option_logit_floor", true);
            }
            if q.kind == "choice" {
                if check {
                    let chosen = a.get("choice").and_then(Value::as_str).unwrap_or("").to_owned();
                    let original = &views[qi][0].1;
                    let variants: Vec<HashMap<String, f64>> = views[qi][1..].iter().map(|(_, p)| p.clone()).collect();
                    let (diag, unstable) = order_diagnostic(original, &variants, &chosen, Some(ORDERS));
                    a.insert("order_diagnostic", diag);
                    if unstable {
                        add_reason(&mut a, "option_order_sensitive", true);
                    }
                } else {
                    disabled(&mut a);
                }
            }
            a.insert("engine", Value::str(Engine::Generalist.name()));
            answers.insert(q.id.clone(), Value::Object(a));
        }
        Ok(Part { answers, tokens })
    }

    /// A decision model (/v1/systemone) on its questions; choice questions are read again with their options
    /// reversed and rotated for the order check (one request per permutation, questions are independent).
    fn systemone(&self, engine: Engine, payload: &Value, questions: &[&Question], threshold: f64, check: bool) -> Result<Part, ApiError> {
        let source = get(payload, &["questions"]).and_then(Value::as_object).ok_or_else(|| bad("Provide 1–32 questions"))?;
        let request = |order: Option<&dyn Fn(usize) -> Vec<usize>>, only_choice: bool| -> Result<Option<Value>, ApiError> {
            let mut qs = Map::new();
            for q in questions {
                if only_choice && q.kind != "choice" {
                    continue;
                }
                let mut spec = source.get(&q.id).cloned().unwrap_or(Value::Null);
                if let (Some(f), Value::Object(m)) = (order, &mut spec) {
                    // the canonical (validated) criteria in the permuted order
                    let perm = f(q.keys.len());
                    let mut c = Map::new();
                    let crit = q.criteria.as_object();
                    for i in perm {
                        let k = &q.keys[i];
                        c.push_unique(k.clone(), crit.and_then(|m| m.get(k)).cloned().unwrap_or(Value::Null));
                    }
                    m.insert("criteria", Value::Object(c));
                }
                qs.push_unique(q.id.clone(), spec);
            }
            if qs.is_empty() {
                return Ok(None);
            }
            let mut part = Map::new();
            part.push_unique("state", get(payload, &["state"]).cloned().unwrap_or(Value::Null));
            part.push_unique("questions", Value::Object(qs));
            let body = crate::app::systemone_request(&Value::Object(part), engine.name()).map_err(bad)?;
            self.post("/v1/systemone", &body).map(Some)
        };
        let reply = request(None, false)?.ok_or_else(|| bad("no question"))?;
        let mut tokens = num(get(&reply, &["usage", "input_tokens"])).unwrap_or(0.0) as i64;
        let mut rows = Vec::with_capacity(questions.len());
        for q in questions {
            let answer = get(&reply, &["answers", &q.id]).ok_or_else(|| upstream(format!("no answer for {}", q.id)))?;
            rows.push(systemone_row(q, answer).map_err(upstream)?);
        }
        let owned: Vec<Question> = questions.iter().map(|q| (*q).clone()).collect();
        let label = if engine == Engine::Clef { NOT_VALIDATED } else { VALIDATED };
        let mut answers = schema::answers(&owned, &rows, threshold, &Value::str(label)).map_err(|e| upstream(e.message))?;
        let mut variants: HashMap<String, Vec<HashMap<String, f64>>> = HashMap::new();
        if check && questions.iter().any(|q| q.kind == "choice") {
            let reverse = |n: usize| (0..n).rev().collect::<Vec<_>>();
            let rotate = direct::rotated;
            for perm in [&reverse as &dyn Fn(usize) -> Vec<usize>, &rotate] {
                let Some(r) = request(Some(perm), true)? else { continue };
                tokens += num(get(&r, &["usage", "input_tokens"])).unwrap_or(0.0) as i64;
                for q in questions.iter().filter(|q| q.kind == "choice") {
                    let answer = get(&r, &["answers", &q.id]).ok_or_else(|| upstream(format!("no answer for {}", q.id)))?;
                    let row = systemone_row(q, answer).map_err(upstream)?;
                    variants.entry(q.id.clone()).or_default().push(probabilities_of(&q.keys, &row));
                }
            }
        }
        for (q, row) in questions.iter().zip(&rows) {
            let Some(Value::Object(mut a)) = answers.get(&q.id).cloned() else { continue };
            if q.kind == "choice" {
                if check {
                    let chosen = a.get("choice").and_then(Value::as_str).unwrap_or("").to_owned();
                    let (diag, unstable) = order_diagnostic(&probabilities_of(&q.keys, row), variants.get(&q.id).map(Vec::as_slice).unwrap_or(&[]), &chosen, None);
                    a.insert("order_diagnostic", diag);
                    if unstable {
                        add_reason(&mut a, "option_order_sensitive", true);
                    }
                } else {
                    disabled(&mut a);
                }
            }
            a.insert("engine", Value::str(engine.name()));
            answers.insert(q.id.clone(), Value::Object(a));
        }
        Ok(Part { answers, tokens })
    }

    /// POST /api/decide: validate, route every question, answer, and merge in request order.
    /// `combine`: questions for the generalist are also read by Clef-Flash (or the specialist) and the two
    /// distributions are combined as a weighted product (the "ensemble" mode of 1.x).
    pub fn evaluate(&self, payload: &Value, force: Option<Engine>, combine: bool) -> Result<Value, ApiError> {
        let began = Instant::now();
        let validated = schema::validate(payload).map_err(|e| bad(e.message))?;
        let check = !matches!(get(payload, &["permutation_check"]), Some(Value::Bool(false)));
        let mut routes = Vec::with_capacity(validated.questions.len());
        for q in &validated.questions {
            routes.push(route(q, &self.loaded, force).map_err(|m| ApiError { status: if force.is_some() { 400 } else { 503 }, message: m })?);
        }
        let mut parts: Vec<(Engine, Part)> = Vec::new();
        let mut tokens_extra = 0i64;
        for engine in [Engine::Generalist, Engine::Specialist, Engine::Clef] {
            let qs: Vec<&Question> = validated.questions.iter().zip(&routes).filter(|(_, e)| **e == engine).map(|(q, _)| q).collect();
            if qs.is_empty() {
                continue;
            }
            let part = match engine {
                Engine::Generalist => self.generalist(&validated.state, &qs, validated.threshold, check)?,
                e => self.systemone(e, payload, &qs, validated.threshold, check)?,
            };
            parts.push((engine, part));
        }
        // combined mode: the generalist's questions read a second time by the catalog model, then merged
        let partner = [Engine::Clef, Engine::Specialist].into_iter().find(|e| self.loaded.contains(e));
        let mut combined: Map = Map::new();
        let mut note: Option<String> = None;
        if let (true, None, Some(other)) = (combine, force, partner) {
            let qs: Vec<&Question> = validated
                .questions
                .iter()
                .zip(&routes)
                .filter(|(q, e)| **e == Engine::Generalist && !(q.kind == "score" && q.options.len() > crate::app::MAX_SCORE_LEVELS))
                .map(|(q, _)| q)
                .collect();
            if !qs.is_empty() {
                match self.systemone(other, payload, &qs, validated.threshold, false) {
                    Ok(second) => {
                        let gen = parts.iter().find(|(e, _)| *e == Engine::Generalist).map(|(_, p)| &p.answers);
                        let weight = if other == Engine::Clef { WEIGHT_WITH_CLEF } else { WEIGHT_WITH_SPECIALIST };
                        let mut chosen = Vec::new();
                        let mut rows = Vec::new();
                        for q in &qs {
                            let (Some(g), Some(o)) = (gen.and_then(|m| m.get(&q.id)), second.answers.get(&q.id)) else { continue };
                            let (Some(pg), Some(po)) = (probs_of(g, &q.keys), probs_of(o, &q.keys)) else { continue };
                            rows.push(product_of_experts(&pg, &po, weight));
                            chosen.push((*q).clone());
                        }
                        let label = if other == Engine::Clef { NOT_VALIDATED } else { "ensemble_local_validation_not_general_guarantee" };
                        let built = schema::answers(&chosen, &rows, validated.threshold, &Value::str(label)).map_err(|e| upstream(e.message))?;
                        tokens_extra = second.tokens;
                        for q in &chosen {
                            let (Some(Value::Object(mut a)), Some(g), Some(o)) = (built.get(&q.id).cloned(), gen.and_then(|m| m.get(&q.id)), second.answers.get(&q.id)) else { continue };
                            let mut reasons = a.get("review_reasons").and_then(Value::as_array).cloned().unwrap_or_default();
                            let mut extra = false;
                            for src in [g, o] {
                                for r in get(src, &["review_reasons"]).and_then(Value::as_array).cloned().unwrap_or_default() {
                                    if r.as_str().is_some_and(|x| !RECOMPUTED_REASONS.contains(&x)) && !reasons.contains(&r) {
                                        reasons.push(r);
                                        extra = true;
                                    }
                                }
                            }
                            a.insert("review_reasons", Value::Array(reasons));
                            if extra {
                                a.insert("recommendation", Value::str("review"));
                            }
                            if let Some(d) = get(g, &["order_diagnostic"]) {
                                a.insert("order_diagnostic", d.clone());
                            }
                            a.insert("engine", Value::str("ensemble"));
                            let mut c = Map::new();
                            c.push_unique("generalist", get(g, &["probabilities"]).cloned().unwrap_or(Value::Null));
                            c.push_unique(other.name(), get(o, &["probabilities"]).cloned().unwrap_or(Value::Null));
                            c.push_unique("generalist_weight", Value::Float(weight));
                            c.push_unique("rule", Value::Str(format!("p ~ generalist^w * {}^(1-w)", other.name())));
                            a.insert("components", Value::Object(c));
                            combined.insert(q.id.clone(), Value::Object(a));
                        }
                    }
                    Err(e) => note = Some(format!("{} unavailable for these questions ({}); generalist answered alone", other.name(), e.message.chars().take(160).collect::<String>())),
                }
            }
        }
        let mut answers = Map::with_capacity(validated.questions.len());
        for q in &validated.questions {
            let a = combined
                .get(&q.id)
                .or_else(|| parts.iter().find_map(|(_, p)| p.answers.get(&q.id)))
                .cloned()
                .ok_or_else(|| upstream(format!("no answer for {}", q.id)))?;
            answers.push_unique(q.id.clone(), a);
        }
        let mut usage = Map::new();
        usage.push_unique("input_tokens", Value::int(parts.iter().map(|(_, p)| p.tokens).sum::<i64>() + tokens_extra));
        usage.push_unique("output_tokens", Value::int(0));
        usage.push_unique("questions", Value::int(validated.questions.len() as i64));
        let catalog = [Engine::Clef, Engine::Specialist].into_iter().find(|e| self.loaded.contains(e));
        let mut routing = Map::new();
        let rule = match (self.loaded.contains(&Engine::Generalist), catalog) {
            (true, Some(c)) if combine && force.is_none() => {
                let w = if c == Engine::Clef { WEIGHT_WITH_CLEF } else { WEIGHT_WITH_SPECIALIST };
                format!(">{} options -> {}, otherwise both (product of experts, generalist weight {w:.2})", direct::MAX_OPTIONS, c.name())
            }
            (true, Some(c)) => format!(">{} options -> {}, otherwise generalist", direct::MAX_OPTIONS, c.name()),
            (true, None) => format!("generalist (questions with more than {} options are refused)", direct::MAX_OPTIONS),
            (false, Some(c)) => format!("{} for every question", c.name()),
            (false, None) => "no model".into(),
        };
        routing.push_unique("rule", Value::Str(rule));
        routing.push_unique("mode", Value::str(if combine && force.is_none() { "ensemble" } else { "route" }));
        routing.push_unique("forced", force.map(|e| Value::str(e.name())).unwrap_or(Value::Null));
        let mut per = Map::new();
        for (q, e) in validated.questions.iter().zip(&routes) {
            per.push_unique(q.id.clone(), Value::str(if combined.contains_key(&q.id) { "ensemble" } else { e.name() }));
        }
        routing.push_unique("questions", Value::Object(per));
        let mut out = Map::new();
        out.push_unique("model", Value::str("typelogic"));
        out.push_unique("version", Value::str(env!("CARGO_PKG_VERSION")));
        out.push_unique("backend", Value::str("llama.cpp"));
        out.push_unique("offline", Value::Bool(true));
        if routes.contains(&Engine::Generalist) {
            out.push_unique("prompt_version", Value::str(direct::PROMPT_VERSION));
        }
        out.push_unique("answers", Value::Object(answers));
        if let Some(n) = note {
            out.push_unique("ensemble_note", Value::Str(n));
        }
        out.push_unique("usage", Value::Object(usage));
        out.push_unique("routing", Value::Object(routing));
        out.push_unique("latency_ms", Value::Float((began.elapsed().as_secs_f64() * 100_000.0).round() / 100.0));
        out.push_unique("external_action_executed", Value::Bool(false));
        Ok(Value::Object(out))
    }
}

fn disabled(a: &mut Map) {
    let mut m = Map::new();
    m.push_unique("checked", Value::Bool(false));
    a.insert("order_diagnostic", Value::Object(m));
    add_reason(a, "order_check_disabled", false);
}

#[cfg(test)]
mod tests {
    use super::*;
    use typelogic_core::schema::validate;

    fn questions(json: &str) -> Vec<Question> { validate(&loads(json, false).unwrap()).unwrap().questions }

    #[test]
    fn routes_like_1x_with_clef_first_for_catalogs() {
        let opts: Vec<String> = (0..20).map(|i| format!("\"o{i}\"")).collect();
        let qs = questions(&format!(
            r#"{{"state":"x","questions":{{"small":{{"type":"choice","instructions":"i","criteria":["a","b"]}},"big":{{"type":"choice","instructions":"i","criteria":[{}]}},"yn":{{"type":"noul","instructions":"i"}}}}}}"#,
            opts.join(",")
        ));
        use Engine::*;
        let r = |loaded: &[Engine], force| qs.iter().map(|q| route(q, loaded, force)).collect::<Vec<_>>();
        assert_eq!(r(&[Generalist, Specialist], None), vec![Ok(Generalist), Ok(Specialist), Ok(Generalist)]);
        assert_eq!(r(&[Generalist, Clef], None), vec![Ok(Generalist), Ok(Clef), Ok(Generalist)]);
        assert_eq!(r(&[Generalist, Specialist, Clef], None)[1], Ok(Clef));
        assert_eq!(r(&[Specialist], None), vec![Ok(Specialist); 3]);
        assert!(r(&[Generalist], None)[1].is_err());
        assert!(r(&[Generalist, Specialist], Some(Generalist))[1].is_err());
        assert!(r(&[Generalist], Some(Clef))[0].is_err());
        assert_eq!(r(&[Generalist, Specialist], Some(Specialist))[0], Ok(Specialist));
    }

    #[test]
    fn systemone_rows_follow_canonical_keys() {
        let qs = questions(r#"{"state":"x","questions":{"c":{"type":"choice","instructions":"i","criteria":["a","b"]},"n":{"type":"noul","instructions":"i"},"s":{"type":"score","instructions":"i","criteria":["lo","hi"]}}}"#);
        let a = loads(r#"{"probabilities":{"b":0.75,"a":0.25}}"#, false).unwrap();
        assert_eq!(systemone_row(&qs[0], &a).unwrap(), vec![0.25, 0.75]);
        assert_eq!(systemone_row(&qs[1], &loads(r#"{"noul":0.8}"#, false).unwrap()).unwrap()[1], 0.8);
        assert_eq!(systemone_row(&qs[2], &loads(r#"{"probabilities":{"0":0.1,"1":0.9}}"#, false).unwrap()).unwrap(), vec![0.1, 0.9]);
    }

    #[test]
    fn product_of_experts_matches_1x() {
        // hybrid.product_of_experts({'a': .8, 'b': .2}, {'a': .4, 'b': .6}, .55) in 1.x
        let p = product_of_experts(&[0.8, 0.2], &[0.4, 0.6], 0.55);
        let a = (0.55 * 0.8f64.ln() + 0.45 * 0.4f64.ln()).exp();
        let b = (0.55 * 0.2f64.ln() + 0.45 * 0.6f64.ln()).exp();
        assert!((p[0] - 0.6410680045870314).abs() < 1e-12 && (p[0] - a / (a + b)).abs() < 1e-12 && (p.iter().sum::<f64>() - 1.0).abs() < 1e-12);
        // zeros are clamped to 1e-12, as in 1.x
        assert!((product_of_experts(&[1.0, 0.0], &[1.0, 0.0], 0.5)[0] - 1.0 / (1.0 + 1e-12)).abs() < 1e-15);
    }

    #[test]
    fn order_diagnostic_flags_flips_and_shifts() {
        let p = |a: f64| HashMap::from([("a".to_string(), a), ("b".to_string(), 1.0 - a)]);
        let (d, unstable) = order_diagnostic(&p(0.9), &[p(0.85), p(0.8)], "a", Some(2));
        assert!(!unstable && dumps(&d, DumpOptions::REPLY).unwrap().contains("\"averaged_orders\": 2"));
        assert!(order_diagnostic(&p(0.9), &[p(0.4)], "a", None).1);
        assert!(order_diagnostic(&p(0.9), &[p(0.7)], "a", None).1);
    }
}
