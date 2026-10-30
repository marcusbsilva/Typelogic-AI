//! Port of `typelogic/titles.py`: the title the user typed, else one generated after the evaluation.
//!
//! The generalist (Plumb, already loaded) writes a title of a few words when it is available (JSON output,
//! temperature 0); otherwise, or when its answer is unusable, the title is the first sentence of the case.
use typelogic_core::json::{dumps, DumpOptions, Value};
use typelogic_core::text::{is_space, is_word, py_len};

pub const MAX_WORDS: usize = 8;
pub const MAX_CHARS: usize = 80;
pub const SYSTEM: &str = "Write a short title for the case the user sends: 3 to 7 words, in the language of the case, naming what the \
                          case is about. No quotes, no final period, no decision or verdict.";
/// Words that should not end a shortened title ("... three days in a" -> "... three days").
const TRAILING: [&str; 32] = [
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "from", "by", "with", "and", "or", "but", "my", "your", "our", "their", "his",
    "her", "its", "is", "are", "was", "were", "be", "that", "which", "who", "as", "than",
];

fn words(s: &str) -> Vec<&str> { s.split(is_space).filter(|w| !w.is_empty()).collect() }

/// Python `str.strip(chars)` / `rstrip(chars)`.
fn strip_chars<'a>(s: &'a str, chars: &str) -> &'a str { s.trim_matches(|c| chars.contains(c)) }
fn rstrip_chars<'a>(s: &'a str, chars: &str) -> &'a str { s.trim_end_matches(|c| chars.contains(c)) }

/// A usable title (one line, trimmed, at most MAX_CHARS) or None.
pub fn clean(value: &str) -> Option<String> {
    let joined = words(value).join(" ");
    let title = rstrip_chars(strip_chars(&joined, " \"'`*#-:"), ".");
    if title.is_empty() || py_len(title) > MAX_CHARS {
        return None;
    }
    let mut chars = title.chars();
    let first = chars.next()?;
    Some(first.to_uppercase().collect::<String>() + chars.as_str())
}

/// `re.split(r'(?<=[.!?])\s+|\n+', text)`.
fn sentences(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let (mut out, mut start, mut i) = (Vec::new(), 0usize, 0usize);
    while i < chars.len() {
        let after_stop = i > 0 && matches!(chars[i - 1], '.' | '!' | '?');
        let end = if after_stop && is_space(chars[i]) {
            let mut j = i;
            while j < chars.len() && is_space(chars[j]) {
                j += 1;
            }
            Some(j)
        } else if chars[i] == '\n' {
            let mut j = i;
            while j < chars.len() && chars[j] == '\n' {
                j += 1;
            }
            Some(j)
        } else {
            None
        };
        match end {
            Some(j) => {
                out.push(chars[start..i].iter().collect());
                start = j;
                i = j;
            }
            None => i += 1,
        }
    }
    out.push(chars[start..].iter().collect());
    out
}

/// `_GREETING.sub('', s)`: `^(hi|hello|hey|dear|good (morning|afternoon|evening))\b[^,.!?]*[,.!?]?\s*`, case-insensitive.
fn drop_greeting(s: &str) -> String {
    let chars: Vec<char> = s.chars().collect();
    let lower: Vec<char> = chars.iter().map(|c| if c.is_ascii() { c.to_ascii_lowercase() } else { *c }).collect();
    let starts = |w: &str| lower.len() >= w.chars().count() && lower.iter().zip(w.chars()).all(|(a, b)| *a == b);
    // alternatives in pattern order (the regex engine tries them in this order)
    for w in ["hi", "hello", "hey", "dear", "good morning", "good afternoon", "good evening"] {
        let n = w.chars().count();
        if !starts(w) {
            continue;
        }
        if n < chars.len() && is_word(chars[n]) {
            continue; // \b fails: "high", "hint"
        }
        let mut j = n;
        while j < chars.len() && !matches!(chars[j], ',' | '.' | '!' | '?') {
            j += 1;
        }
        if j < chars.len() {
            j += 1;
        }
        while j < chars.len() && is_space(chars[j]) {
            j += 1;
        }
        return chars[j..].iter().collect();
    }
    s.to_owned()
}

/// The first sentence of the case with at least three words, cut to MAX_WORDS words.
pub fn from_text(text: &Value) -> String {
    let text = match text {
        Value::Str(s) => s.clone(),
        other => dumps(other, DumpOptions { sort_keys: false, compact: false, ensure_ascii: false, allow_nan: true }).unwrap_or_default(),
    };
    let parts: Vec<String> = sentences(&text).into_iter().map(|s| typelogic_core::text::strip(&s).to_owned()).filter(|s| !s.is_empty()).collect();
    let chosen = parts.iter().find(|s| words(s).len() >= 3).cloned().or_else(|| parts.first().cloned()).unwrap_or_else(|| "Decision".into());
    let greeted = drop_greeting(&chosen);
    let chosen = { let g = typelogic_core::text::strip(&greeted); if g.is_empty() { chosen.clone() } else { g.to_owned() } };
    let mut w = words(&chosen);
    if w.len() > MAX_WORDS {
        w.truncate(MAX_WORDS);
        while w.len() > 3 && TRAILING.contains(&strip_chars(&w[w.len() - 1].to_lowercase(), ",;:")) {
            w.pop();
        }
    }
    let joined = w.join(" ");
    let mut title = rstrip_chars(&joined, ",;:").to_owned();
    if py_len(&title) > MAX_CHARS {
        let cut: String = title.chars().take(MAX_CHARS).collect();
        title = match cut.rfind(' ') { Some(i) => cut[..i].to_owned(), None => cut };
    }
    clean(&title).unwrap_or_else(|| "Decision".into())
}

/// A title from the model's JSON answer, when usable (at most MAX_WORDS + 4 words).
pub fn from_model(content: &str) -> Option<String> {
    let v = typelogic_core::json::loads(content, false).ok()?;
    let t = clean(v.as_object()?.get("title")?.as_str()?)?;
    (words(&t).len() <= MAX_WORDS + 4).then_some(t)
}

#[cfg(test)]
mod tests {
    use super::*;
    use typelogic_core::json::loads;

    #[test]
    fn matches_the_1x_titles() {
        let fixture = loads(include_str!("../tests/titles_fixture.json"), false).unwrap();
        let rows = fixture.as_array().unwrap();
        assert!(rows.len() > 100);
        for r in rows {
            let r = r.as_object().unwrap();
            assert_eq!(from_text(r.get("text").unwrap()), r.get("title").unwrap().as_str().unwrap(), "{:?}", r.get("text"));
        }
    }

    #[test]
    fn model_titles() {
        assert_eq!(from_model(r#"{"title": " \"failed payout three days\" "}"#).as_deref(), Some("Failed payout three days")); // as 1.x clean()
        assert_eq!(from_model("not json"), None);
        assert_eq!(from_model(r#"{"title": "one two three four five six seven eight nine ten eleven twelve thirteen"}"#), None);
    }
}
