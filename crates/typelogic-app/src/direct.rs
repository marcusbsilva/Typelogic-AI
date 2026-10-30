//! Direct option-letter readout of the generalist (Plumb): the prompt of TypeLogic 1.x `typelogic/direct.py`
//! (port of SemIf's direct readout, MIT), rendered with the Qwen3.5 chat template exactly as the 1.x tokenizer does.
//! The model reads the state, the criterion and lettered options in one JSON message; the next-token
//! log-probabilities of the declared letters, softmaxed, are the answer. No token is generated.
//!
//! Byte-identical prompts are checked against the 1.x Python output in `tests/direct_fixture.json`.
use typelogic_core::json::{dumps, loads, DumpOptions, Map, Value};
use typelogic_core::schema::Question;
use typelogic_core::text::strip;

pub const LETTERS: [&str; 16] = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P"];
/// Most options the letter readout supports; larger questions go to a decision model.
pub const MAX_OPTIONS: usize = LETTERS.len();
pub const SYSTEM: &str = "Apply the supplied criterion to the supplied evidence. Choose exactly one listed option. \
                          Respond with only its uppercase letter, with no explanation or reasoning.";
pub const PROMPT_VERSION: &str = "semif-direct-options-v1";

/// `json.dumps(value, ensure_ascii=False)`: default separators, insertion order, NaN/Infinity allowed.
const PY_DUMPS: DumpOptions = DumpOptions { sort_keys: false, compact: false, ensure_ascii: false, allow_nan: true };

/// (option ids in prompt order, "<id>: <text>" descriptions) of a validated question; `order` permutes the canonical order.
pub fn options_of(q: &Question, order: &[usize]) -> Result<(Vec<String>, Vec<String>), String> {
    let criteria = q.criteria.as_object();
    let text_of = |k: &str| criteria.and_then(|m| m.get(k)).and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned);
    let (ids, texts): (Vec<String>, Vec<String>) = match q.kind.as_str() {
        "noul" => {
            let ids = vec!["true".to_owned(), "false".to_owned()];
            let texts = ids.iter().map(|k| text_of(k).unwrap_or_else(|| format!("The proposition is {k}."))).collect();
            (ids, texts)
        }
        "choice" => (q.keys.clone(), q.keys.iter().map(|k| text_of(k).unwrap_or_else(|| k.clone())).collect()),
        _ => (q.keys.clone(), q.options.clone()),
    };
    if !(2..=MAX_OPTIONS).contains(&ids.len()) {
        return Err(format!("Direct readout supports 2-{MAX_OPTIONS} options"));
    }
    let mut sorted = order.to_vec();
    sorted.sort_unstable();
    if sorted != (0..ids.len()).collect::<Vec<_>>() {
        return Err("order must be a permutation of the option indices".into());
    }
    let ids: Vec<String> = order.iter().map(|&i| ids[i].clone()).collect();
    let descriptions = order.iter().zip(&ids).map(|(&i, id)| format!("{id}: {}", texts[i])).collect();
    Ok((ids, descriptions))
}

/// Number of options the readout presents for a question (noul: 2).
pub fn option_count(q: &Question) -> usize { if q.kind == "noul" { 2 } else { q.keys.len() } }

/// A JSON object or array given as text is passed as structured evidence (as SemIf does), anything else as text.
fn structured_state(state: &str) -> Value {
    let text = strip(state);
    if text.starts_with('{') || text.starts_with('[') {
        if let Ok(v) = loads(text, false) {
            return v;
        }
    }
    Value::str(state)
}

/// The chat-formatted prompt (Qwen3.5 template, generation prompt, thinking disabled) and the option ids in prompt order.
pub fn prompt(state: &str, q: &Question, order: &[usize]) -> Result<(String, Vec<String>), String> {
    let (ids, descriptions) = options_of(q, order)?;
    let mut payload = Map::with_capacity(3);
    payload.push_unique("evidence", structured_state(state));
    payload.push_unique("criterion", Value::str(&q.instr));
    let options = descriptions
        .into_iter()
        .enumerate()
        .map(|(i, d)| {
            let mut m = Map::with_capacity(2);
            m.push_unique("letter", Value::str(LETTERS[i]));
            m.push_unique("description", Value::Str(d));
            Value::Object(m)
        })
        .collect();
    payload.push_unique("options", Value::Array(options));
    let user = dumps(&Value::Object(payload), PY_DUMPS).map_err(|e| e.message)?;
    let text = format!(
        "<|im_start|>system\n{SYSTEM}<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    );
    Ok((text, ids))
}

/// Canonical order, plus the reversed order with two-order averaging.
pub fn view_orders(count: usize, orders: usize) -> Vec<Vec<usize>> {
    let mut v = vec![(0..count).collect::<Vec<_>>()];
    if orders == 2 {
        v.push((0..count).rev().collect());
    }
    v
}

/// The order of the extra reading of the permutation diagnostic: rotated by one.
pub fn rotated(count: usize) -> Vec<usize> { (1..count).chain(std::iter::once(0)).collect() }

pub fn softmax(values: &[f64], temperature: f64) -> Result<Vec<f64>, String> {
    if values.len() < 2 || values.iter().any(|v| !v.is_finite()) {
        return Err("Need at least two finite option logits".into());
    }
    let scaled: Vec<f64> = values.iter().map(|v| v / temperature).collect();
    let top = scaled.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let w: Vec<f64> = scaled.iter().map(|v| (v - top).exp()).collect();
    let total: f64 = w.iter().sum();
    Ok(w.into_iter().map(|x| x / total).collect())
}

/// Length of the token prefix shared by every prompt.
pub fn common_prefix(prompts: &[Vec<i64>]) -> usize {
    let Some(first) = prompts.first() else { return 0 };
    let mut n = prompts.iter().map(Vec::len).min().unwrap_or(0);
    for p in &prompts[1..] {
        n = n.min(first.iter().zip(p).take_while(|(a, b)| a == b).count());
    }
    n
}

#[cfg(test)]
mod tests {
    use super::*;
    use typelogic_core::schema::validate;

    /// Prompts of the 1.x Python readout (`spike/work/mkfix.py`) over JevBench and test2: every question type, text
    /// and structured states, ASCII and non-ASCII evidence, canonical and reversed option orders.
    #[test]
    fn prompts_match_the_1x_readout_byte_for_byte() {
        let fixture = loads(include_str!("../tests/direct_fixture.json"), false).unwrap();
        let cases = fixture.as_array().unwrap();
        assert!(cases.len() >= 50);
        for case in cases {
            let c = case.as_object().unwrap();
            let v = validate(c.get("payload").unwrap()).unwrap();
            let qid = c.get("question").unwrap().as_str().unwrap();
            let q = v.questions.iter().find(|q| q.id == qid).unwrap();
            let order: Vec<usize> = c.get("order").unwrap().as_array().unwrap().iter().map(|x| x.to_f64().unwrap() as usize).collect();
            let (text, ids) = prompt(&v.state, q, &order).unwrap();
            let want_ids: Vec<&str> = c.get("option_ids").unwrap().as_array().unwrap().iter().map(|x| x.as_str().unwrap()).collect();
            assert_eq!(ids, want_ids, "{}", c.get("id").unwrap().as_str().unwrap());
            assert_eq!(text, c.get("prompt").unwrap().as_str().unwrap(), "{}", c.get("id").unwrap().as_str().unwrap());
        }
    }

    #[test]
    fn limits_and_helpers() {
        let v = validate(&loads(r#"{"state":"x","questions":{"q":{"type":"choice","instructions":"i","criteria":["a","b","c"]}}}"#, false).unwrap()).unwrap();
        let q = &v.questions[0];
        assert!(options_of(q, &[0, 0, 1]).is_err());
        assert_eq!(options_of(q, &[2, 0, 1]).unwrap().0, vec!["c", "a", "b"]);
        assert_eq!(rotated(3), vec![1, 2, 0]);
        assert_eq!(view_orders(3, 2), vec![vec![0, 1, 2], vec![2, 1, 0]]);
        let p = softmax(&[0.0, 1.0f64.ln()], 1.0).unwrap();
        assert!((p[0] - 0.5).abs() < 1e-12);
        assert_eq!(common_prefix(&[vec![1, 2, 3], vec![1, 2, 4], vec![1, 2]]), 2);
        let many: Vec<String> = (0..17).map(|i| format!("\"o{i}\"")).collect();
        let big = validate(&loads(&format!(r#"{{"state":"x","questions":{{"q":{{"type":"choice","instructions":"i","criteria":[{}]}}}}}}"#, many.join(",")), false).unwrap()).unwrap();
        assert!(options_of(&big.questions[0], &(0..17).collect::<Vec<_>>()).is_err());
    }
}
