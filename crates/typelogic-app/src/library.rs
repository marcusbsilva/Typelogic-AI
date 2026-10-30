//! The decision library (port of `typelogic/store.py`): decisions with title and tags, corrections and the audit
//! trail, in `data/library/<id>.json` (one file per decision; at most 200 are kept, see archive.rs).
//!
//! Each record keeps the SHA-512 and MD5 of its canonical form (typelogic-core, byte-compatible with 1.x), so a file
//! changed outside the application is reported as unverified instead of being trusted. A file that cannot be read is
//! listed as unreadable, so it can still be inspected and deleted.
use crate::util::{now_iso, uuid4_hex, write_atomic};
use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use typelogic_core::exports;
use typelogic_core::hash::{hex, sha256};
use typelogic_core::json::{dumps, loads, DumpOptions, Map, Value};
use typelogic_core::text::py_len;

const FORMAT: &str = "typelogic-library-v2";
/// Pretty JSON with the key order kept (the files are meant to be readable).
const FILE_JSON: DumpOptions = DumpOptions { sort_keys: false, compact: false, ensure_ascii: false, allow_nan: false };

type R<T> = Result<T, String>;

/// One decision as stored: the record of 1.x plus its corrections and audit rows.
#[derive(Debug, Clone)]
struct Entry {
    seq: u64,
    /// id, space, title, created, request, response, sha512, md5, baseline_origin
    record: Map,
    feedback: Vec<Map>,
    audit: Vec<Map>,
    /// set when the file could not be read
    unreadable: Option<String>,
}

impl Entry {
    fn id(&self) -> &str { self.record.get("id").and_then(Value::as_str).unwrap_or("") }
    fn space(&self) -> &str { self.record.get("space").and_then(Value::as_str).unwrap_or("") }
    fn created(&self) -> &str { self.record.get("created").and_then(Value::as_str).unwrap_or("") }

    fn to_file(&self) -> Value {
        let mut m = Map::new();
        m.push_unique("format", Value::str(FORMAT));
        m.push_unique("seq", Value::int(self.seq as i64));
        for (k, v) in self.record.iter() {
            m.push_unique(k.clone(), v.clone());
        }
        m.push_unique("feedback", Value::Array(self.feedback.iter().cloned().map(Value::Object).collect()));
        m.push_unique("audit", Value::Array(self.audit.iter().cloned().map(Value::Object).collect()));
        Value::Object(m)
    }

    fn from_file(path: &Path) -> Entry {
        let fallback_id = path.file_stem().and_then(|s| s.to_str()).unwrap_or("").to_owned();
        let parsed = fs::read_to_string(path).map_err(|e| e.to_string()).and_then(|t| loads(&t, true).map_err(|e| e.message));
        let mut entry = Entry { seq: 0, record: Map::new(), feedback: Vec::new(), audit: Vec::new(), unreadable: None };
        match parsed {
            Ok(Value::Object(m)) => {
                for (k, v) in m.into_entries() {
                    match k.as_str() {
                        "format" => {}
                        "seq" => entry.seq = v.to_f64().unwrap_or(0.0) as u64,
                        "feedback" => entry.feedback = v.as_array().map(|a| a.iter().filter_map(|x| x.as_object().cloned()).collect()).unwrap_or_default(),
                        "audit" => entry.audit = v.as_array().map(|a| a.iter().filter_map(|x| x.as_object().cloned()).collect()).unwrap_or_default(),
                        _ => entry.record.push_unique(k, v),
                    }
                }
                if entry.record.get("id").and_then(Value::as_str) != Some(fallback_id.as_str()) {
                    entry.unreadable = Some("the file name and the record id differ".into());
                }
            }
            Ok(_) => entry.unreadable = Some("the file is not a JSON object".into()),
            Err(e) => entry.unreadable = Some(e.chars().take(200).collect()),
        }
        if entry.unreadable.is_some() {
            entry.record.insert("id", Value::Str(fallback_id));
            for k in ["space", "title", "created"] {
                if !entry.record.get(k).is_some_and(|v| v.as_str().is_some()) {
                    entry.record.insert(k, Value::str(if k == "space" { "default" } else { "" }));
                }
            }
        }
        entry
    }

    /// `Store.decode_record`: the public record (id, space, title, request, response, created, integrity).
    fn decoded(&self) -> Map {
        let mut out = Map::new();
        for k in ["id", "space", "title", "request", "response", "created"] {
            out.push_unique(k, self.record.get(k).cloned().unwrap_or(Value::Null));
        }
        let shapes_ok = matches!(out.get("request"), Some(Value::Object(_))) && matches!(out.get("response"), Some(Value::Object(_)));
        let integrity = match (&self.unreadable, shapes_ok) {
            (None, true) => exports::verification(&self.record).map_err(|e| e.message),
            (Some(e), _) => Err(e.clone()),
            (None, false) => Err("ValueError: request and response must be JSON objects".into()),
        };
        match integrity {
            Ok(v) => out.insert("integrity", Value::Object(v)),
            Err(e) => {
                for name in ["request", "response"] {
                    if !matches!(out.get(name), Some(Value::Object(_))) {
                        let mut m = Map::new();
                        let text = out.get(name).map(|v| dumps(v, DumpOptions::REPLY).unwrap_or_default()).unwrap_or_default();
                        m.push_unique("unreadable", Value::Str(text.chars().take(2000).collect()));
                        out.insert(name, Value::Object(m));
                    }
                }
                if let Some(Value::Object(mut r)) = out.get("response").cloned() {
                    if !r.contains_key("answers") {
                        r.push_unique("answers", Value::Object(Map::new()));
                    }
                    out.insert("response", Value::Object(r));
                }
                let mut i = Map::new();
                i.push_unique("verified", Value::Bool(false));
                i.push_unique("error", Value::Str(format!("Stored record is unreadable: {e}")));
                out.insert("integrity", Value::Object(i));
            }
        }
        out
    }

    fn verified(&self) -> bool { self.decoded().get("integrity").and_then(|i| i.as_object()?.get("verified").cloned()) == Some(Value::Bool(true)) }
}

pub struct Library {
    dir: PathBuf,
    entries: Vec<Entry>,
    next_seq: u64,
    next_audit: u64,
}

fn audit_row(id: u64, decision: &str, action: &str, details: Value) -> Map {
    let mut a = Map::new();
    a.push_unique("id", Value::int(id as i64));
    a.push_unique("decision_id", Value::str(decision));
    a.push_unique("action", Value::str(action));
    a.push_unique("details", details);
    a.push_unique("created", Value::Str(now_iso()));
    a
}

fn check_id(key: Option<&Value>) -> R<String> {
    match key.and_then(Value::as_str) {
        Some(k) if (1..=128).contains(&py_len(k)) => Ok(k.to_owned()),
        _ => Err("Invalid decision id".into()),
    }
}

/// A decision id usable as a file name (ids are uuid hex; imported ones are checked too).
fn safe_id(id: &str) -> bool { !id.is_empty() && id.len() <= 128 && id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_') }

impl Library {
    /// Open (and read) the library folder.
    pub fn open(dir: &Path) -> io::Result<Library> {
        fs::create_dir_all(dir)?;
        let mut entries = Vec::new();
        for item in fs::read_dir(dir)? {
            let path = item?.path();
            if path.extension().and_then(|e| e.to_str()) == Some("json") {
                entries.push(Entry::from_file(&path));
            }
        }
        let next_seq = entries.iter().map(|e| e.seq).max().unwrap_or(0) + 1;
        let next_audit = entries.iter().flat_map(|e| e.audit.iter()).filter_map(|a| a.get("id")?.to_f64().ok()).fold(0.0, f64::max) as u64 + 1;
        Ok(Library { dir: dir.to_path_buf(), entries, next_seq, next_audit })
    }

    fn path(&self, id: &str) -> PathBuf { self.dir.join(format!("{id}.json")) }

    fn save(&self, e: &Entry) -> R<()> {
        let text = typelogic_core::json::dumps_indent(&e.to_file(), 1, FILE_JSON).map_err(|x| x.message)?;
        write_atomic(&self.path(e.id()), text.as_bytes()).map_err(|x| format!("could not write the library: {x}"))
    }

    pub fn count(&self) -> usize { self.entries.len() }

    /// Newest first (`ORDER BY created DESC, rowid DESC`).
    fn ordered(&self) -> Vec<&Entry> {
        let mut v: Vec<&Entry> = self.entries.iter().collect();
        v.sort_by(|a, b| b.created().cmp(a.created()).then(b.seq.cmp(&a.seq)));
        v
    }

    pub fn add(&mut self, request: Value, response: Value, title: &str, space: &str) -> R<String> {
        let id = uuid4_hex();
        let mut record = Map::new();
        record.push_unique("id", Value::str(&id));
        record.push_unique("space", Value::str(space));
        record.push_unique("title", Value::str(title));
        record.push_unique("created", Value::Str(now_iso()));
        record.push_unique("request", request);
        record.push_unique("response", response);
        let (sha512, md5) = exports::digests(&record).map_err(|e| e.message)?;
        record.push_unique("sha512", Value::Str(sha512));
        record.push_unique("md5", Value::Str(md5));
        record.push_unique("baseline_origin", Value::str("created"));
        let audit = vec![audit_row(self.next_audit, &id, "created", Value::Object(Map::new()))];
        let e = Entry { seq: self.next_seq, record, feedback: Vec::new(), audit, unreadable: None };
        self.save(&e)?;
        self.next_seq += 1;
        self.next_audit += 1;
        self.entries.push(e);
        Ok(id)
    }

    /// Public records, newest first; `space` None means every workspace.
    pub fn decisions(&self, space: Option<&str>) -> Vec<Value> {
        self.ordered().into_iter().filter(|e| space.is_none_or(|s| e.space() == s)).map(|e| Value::Object(e.decoded())).collect()
    }

    pub fn get(&self, key: &str, require_verified: bool) -> R<Map> {
        let e = self.entries.iter().find(|e| e.id() == key).ok_or("Decision not found")?;
        let d = e.decoded();
        if require_verified && !e.verified() {
            return Err("Decision integrity verification failed".into());
        }
        Ok(d)
    }

    pub fn export_decision(&self, key: &str, format: &str) -> R<Value> {
        if format != "full" && format != "lite" {
            return Err("Unknown export format".into());
        }
        let record = self.get(key, true)?;
        if format == "lite" {
            return exports::lite(&record).map(Value::Object).map_err(|e| e.message);
        }
        Ok(Value::Object(record))
    }

    pub fn delete(&mut self, key: Option<&Value>) -> R<Value> {
        let key = check_id(key)?;
        let found = self.entries.iter().position(|e| e.id() == key);
        if let Some(i) = found {
            if safe_id(&key) {
                match fs::remove_file(self.path(&key)) {
                    Ok(()) => {}
                    Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                    Err(e) => return Err(format!("could not delete {key}: {e}")),
                }
            }
            self.entries.remove(i);
        }
        let mut m = Map::new();
        m.push_unique("id", Value::Str(key));
        m.push_unique("deleted", Value::Bool(found.is_some()));
        m.push_unique("external_action_executed", Value::Bool(false));
        Ok(Value::Object(m))
    }

    pub fn delete_many(&mut self, ids: Option<&Value>) -> R<Value> {
        let list = match ids {
            Some(Value::Array(a)) if (1..=500).contains(&a.len()) => a.clone(),
            _ => return Err("ids: a list of 1-500 distinct decision ids".into()),
        };
        let texts: Vec<String> = list.iter().map(|v| v.as_str().map(str::to_owned).unwrap_or_else(|| dumps(v, DumpOptions::REPLY).unwrap_or_default())).collect();
        if (0..texts.len()).any(|i| texts[..i].contains(&texts[i])) {
            return Err("ids: a list of 1-500 distinct decision ids".into());
        }
        let mut results = Vec::new();
        let mut deleted = 0;
        for key in &list {
            match self.delete(Some(key)) {
                Ok(r) => {
                    if r.as_object().and_then(|m| m.get("deleted")) == Some(&Value::Bool(true)) {
                        deleted += 1;
                    }
                    results.push(r);
                }
                Err(e) => {
                    let mut m = Map::new();
                    m.push_unique("id", key.clone());
                    m.push_unique("error", Value::Str(e));
                    results.push(Value::Object(m));
                }
            }
        }
        let mut m = Map::new();
        m.push_unique("results", Value::Array(results));
        m.push_unique("deleted", Value::int(deleted));
        m.push_unique("external_action_executed", Value::Bool(false));
        Ok(Value::Object(m))
    }

    /// A correction: the target distribution of one answer (`Store.feedback`).
    pub fn feedback(&mut self, key: Option<&Value>, question: Option<&Value>, target: Option<&Value>) -> R<Value> {
        let (Some(key), Some(question)) = (key.and_then(Value::as_str).filter(|k| (1..=128).contains(&py_len(k))), question.and_then(Value::as_str)) else {
            return Err("Invalid decision or question id".into());
        };
        self.get(key, true)?;
        let i = self.entries.iter().position(|e| e.id() == key).ok_or("Decision not found")?;
        let answer = self.entries[i].record.get("response").and_then(|r| r.as_object()?.get("answers")?.as_object()?.get(question).cloned());
        let probs: Vec<String> = answer.as_ref().and_then(|a| a.as_object()?.get("probabilities")?.as_object().map(|p| p.keys().cloned().collect())).unwrap_or_default();
        let valid = match (answer.as_ref(), target.and_then(Value::as_object)) {
            (Some(_), Some(t)) => {
                let mut keys: Vec<&String> = t.keys().collect();
                keys.sort();
                let mut want: Vec<&String> = probs.iter().collect();
                want.sort();
                want.dedup();
                let values: Vec<Option<f64>> = t.iter().map(|(_, v)| match v {
                    Value::Int(_) | Value::Float(_) => v.to_f64().ok().filter(|f| f.is_finite() && *f >= 0.0),
                    _ => None,
                }).collect();
                keys == want && values.iter().all(Option::is_some) && (values.iter().map(|v| v.unwrap_or(0.0)).sum::<f64>() - 1.0).abs() <= 1e-6
            }
            _ => false,
        };
        if !valid {
            return Err("Invalid target distribution".into());
        }
        let mut f = Map::new();
        f.push_unique("id", Value::Str(uuid4_hex()));
        f.push_unique("decision_id", Value::str(key));
        f.push_unique("question_id", Value::str(question));
        f.push_unique("target", target.cloned().unwrap_or(Value::Null));
        f.push_unique("created", Value::Str(now_iso()));
        let mut details = Map::new();
        details.push_unique("question_id", Value::str(question));
        let audit = audit_row(self.next_audit, key, "feedback", Value::Object(details));
        let mut e = self.entries[i].clone();
        e.feedback.push(f);
        e.audit.push(audit);
        self.save(&e)?;
        self.entries[i] = e;
        self.next_audit += 1;
        let mut m = Map::new();
        m.push_unique("saved", Value::Bool(true));
        m.push_unique("retrained", Value::Bool(false));
        Ok(Value::Object(m))
    }

    pub fn stats(&self) -> Value {
        let mut m = Map::new();
        m.push_unique("decisions", Value::int(self.count() as i64));
        m.push_unique("feedback", Value::int(self.entries.iter().map(|e| e.feedback.len()).sum::<usize>() as i64));
        Value::Object(m)
    }

    /// Feedback and audit rows in the 1.x export shape (target and details as canonical JSON text).
    fn related_rows(&self, e: &Entry, text: bool) -> (Vec<Value>, Vec<Value>) {
        let as_text = |v: &Value| if text { Value::Str(dumps(v, DumpOptions::CANONICAL).unwrap_or_default()) } else { v.clone() };
        let feedback = e.feedback.iter().map(|f| {
            let mut m = f.clone();
            if let Some(t) = f.get("target") {
                m.insert("target", as_text(t));
            }
            Value::Object(m)
        }).collect();
        let audit = e.audit.iter().map(|a| {
            let mut m = a.clone();
            if let Some(d) = a.get("details") {
                m.insert("details", as_text(d));
            }
            Value::Object(m)
        }).collect();
        (feedback, audit)
    }

    /// `Store.export`: every decision, correction and audit row (the format the importer reads back).
    pub fn export(&self) -> Value {
        let (mut feedback, mut audit) = (Vec::new(), Vec::new());
        for e in self.ordered() {
            let (f, a) = self.related_rows(e, true);
            feedback.extend(f);
            audit.extend(a);
        }
        let mut m = Map::new();
        m.push_unique("decisions", Value::Array(self.decisions(None)));
        m.push_unique("feedback", Value::Array(feedback));
        m.push_unique("audit", Value::Array(audit));
        Value::Object(m)
    }

    /// Each decision with its corrections and audit (parsed), for the archive; newest first.
    pub fn archive_rows(&self) -> Vec<(String, String, Value)> {
        self.ordered()
            .into_iter()
            .map(|e| {
                let mut d = e.decoded();
                let (f, a) = self.related_rows(e, false);
                d.push_unique("feedback", Value::Array(f));
                d.push_unique("audit", Value::Array(a));
                (e.id().to_owned(), e.space().to_owned(), Value::Object(d))
            })
            .collect()
    }

    /// Remove decisions after they were archived.
    pub fn remove_all(&mut self, ids: &[String]) -> R<()> {
        for id in ids {
            self.delete(Some(&Value::str(id)))?;
        }
        Ok(())
    }

    /// `Store.training_records`: corrected decisions as training rows, merged by state.
    pub fn training_records(&self, space: Option<&str>) -> Value {
        let mut merged: Vec<(String, Map)> = Vec::new();
        let (mut integrity_failed, mut uncorrected) = (0i64, 0i64);
        let mut rows: Vec<&Entry> = self.ordered().into_iter().filter(|e| space.is_none_or(|s| e.space() == s)).collect();
        rows.reverse(); // oldest first: later reviews win
        for e in rows {
            if !e.verified() {
                integrity_failed += 1;
                continue;
            }
            // latest correction per question
            let mut fixed: BTreeMap<String, Value> = BTreeMap::new();
            let mut fb: Vec<&Map> = e.feedback.iter().collect();
            fb.sort_by(|a, b| a.get("created").and_then(Value::as_str).cmp(&b.get("created").and_then(Value::as_str)));
            for f in fb {
                if let (Some(q), Some(t)) = (f.get("question_id").and_then(Value::as_str), f.get("target")) {
                    fixed.insert(q.to_owned(), t.clone());
                }
            }
            let request = e.record.get("request").and_then(Value::as_object).cloned().unwrap_or_default();
            let questions = request.get("questions").and_then(Value::as_object).cloned().unwrap_or_default();
            let answers = e.record.get("response").and_then(|r| r.as_object()?.get("answers")?.as_object().cloned()).unwrap_or_default();
            let found: Vec<(String, Value)> = answers
                .iter()
                .filter(|(q, a)| questions.contains_key(q) && a.as_object().and_then(|a| a.get("probabilities")).is_some_and(Value::truthy))
                .filter_map(|(q, _)| fixed.get(q).map(|t| (q.clone(), t.clone())))
                .collect();
            if found.is_empty() {
                uncorrected += 1;
                continue;
            }
            let state = request.get("state").cloned().unwrap_or(Value::Null);
            let group = format!("state-{}", &hex(&sha256(dumps(&state, DumpOptions::CANONICAL).unwrap_or_default().as_bytes()))[..24]);
            let idx = match merged.iter().position(|(g, _)| *g == group) {
                Some(i) => i,
                None => {
                    let mut row = Map::new();
                    row.push_unique("id", Value::str(&group));
                    row.push_unique("group_id", Value::str(&group));
                    row.push_unique("state", state.clone());
                    row.push_unique("questions", Value::Object(Map::new()));
                    row.push_unique("targets", Value::Object(Map::new()));
                    row.push_unique("source_id", Value::str("typelogic-feedback"));
                    row.push_unique("domain", Value::str(e.space()));
                    row.push_unique("provenance", Value::Object(Map::new()));
                    row.push_unique("decisions", Value::Array(Vec::new()));
                    merged.push((group.clone(), row));
                    merged.len() - 1
                }
            };
            let row = &mut merged[idx].1;
            let mut decisions = row.get("decisions").and_then(Value::as_array).cloned().unwrap_or_default();
            decisions.push(Value::str(e.id()));
            row.insert("decisions", Value::Array(decisions));
            let mut qs = row.get("questions").and_then(Value::as_object).cloned().unwrap_or_default();
            let mut ts = row.get("targets").and_then(Value::as_object).cloned().unwrap_or_default();
            let mut ps = row.get("provenance").and_then(Value::as_object).cloned().unwrap_or_default();
            for (qid, target) in found {
                let spec = questions.get(&qid).cloned().unwrap_or(Value::Null);
                let (mut key, mut n) = (qid.clone(), 2);
                while qs.get(&key).is_some_and(|s| *s != spec) {
                    key = format!("{qid}_{n}");
                    n += 1;
                }
                qs.insert(key.clone(), spec);
                ts.insert(key.clone(), target);
                ps.insert(key, Value::str("correction"));
            }
            row.insert("questions", Value::Object(qs));
            row.insert("targets", Value::Object(ts));
            row.insert("provenance", Value::Object(ps));
        }
        let corrections: usize = merged.iter().map(|(_, r)| r.get("provenance").and_then(Value::as_object).map(|p| p.len()).unwrap_or(0)).sum();
        let mut skipped = Map::new();
        skipped.push_unique("integrity_failed", Value::int(integrity_failed));
        skipped.push_unique("uncorrected", Value::int(uncorrected));
        let mut m = Map::new();
        m.push_unique("records", Value::Array(merged.into_iter().map(|(_, r)| Value::Object(r)).collect()));
        m.push_unique("skipped", Value::Object(skipped));
        m.push_unique("corrections", Value::int(corrections as i64));
        Value::Object(m)
    }

    /// Import a 1.x export (Library > Export all, `typelogic-export.json`): verified decisions with their
    /// corrections and audit rows; decisions already present, or failing their integrity check, are skipped.
    pub fn import(&mut self, export: &Value, limit: usize) -> R<Value> {
        let root = export.as_object().ok_or("The file is not a TypeLogic export (a JSON object with decisions)")?;
        let decisions = root.get("decisions").and_then(Value::as_array).ok_or("The file is not a TypeLogic export (no decisions list)")?;
        let rows_of = |name: &str| -> Vec<Map> { root.get(name).and_then(Value::as_array).map(|a| a.iter().filter_map(|x| x.as_object().cloned()).collect()).unwrap_or_default() };
        let (feedback, audit) = (rows_of("feedback"), rows_of("audit"));
        let parse = |v: Option<&Value>| match v {
            Some(Value::Str(s)) => loads(s, true).unwrap_or(Value::Str(s.clone())),
            Some(v) => v.clone(),
            None => Value::Null,
        };
        let mut items: Vec<&Map> = decisions.iter().filter_map(Value::as_object).collect();
        items.sort_by(|a, b| a.get("created").and_then(Value::as_str).cmp(&b.get("created").and_then(Value::as_str)));
        let (mut imported, mut present, mut unverified, mut no_room) = (0i64, 0i64, 0i64, 0i64);
        for d in items {
            let id = d.get("id").and_then(Value::as_str).unwrap_or("").to_owned();
            if !safe_id(&id) {
                unverified += 1;
                continue;
            }
            if self.entries.iter().any(|e| e.id() == id) {
                present += 1;
                continue;
            }
            if self.count() >= limit {
                no_room += 1;
                continue;
            }
            let integrity = d.get("integrity").and_then(Value::as_object);
            let mut record = Map::new();
            for k in ["id", "space", "title", "created", "request", "response"] {
                record.push_unique(k, d.get(k).cloned().unwrap_or(Value::Null));
            }
            for k in ["sha512", "md5"] {
                record.push_unique(k, integrity.and_then(|i| i.get(k)).cloned().unwrap_or(Value::Null));
            }
            record.push_unique("baseline_origin", integrity.and_then(|i| i.get("baseline_origin")).cloned().unwrap_or(Value::str("unknown")));
            let ok = exports::verification(&record).ok().and_then(|v| v.get("verified").cloned()) == Some(Value::Bool(true));
            if !ok {
                unverified += 1;
                continue;
            }
            let mut e = Entry { seq: self.next_seq, record, feedback: Vec::new(), audit: Vec::new(), unreadable: None };
            for f in feedback.iter().filter(|f| f.get("decision_id").and_then(Value::as_str) == Some(id.as_str())) {
                let mut f = f.clone();
                f.insert("target", parse(f.get("target")));
                e.feedback.push(f);
            }
            for a in audit.iter().filter(|a| a.get("decision_id").and_then(Value::as_str) == Some(id.as_str())) {
                let mut a = a.clone();
                a.insert("id", Value::int(self.next_audit as i64));
                a.insert("details", parse(a.get("details")));
                self.next_audit += 1;
                e.audit.push(a);
            }
            e.audit.push(audit_row(self.next_audit, &id, "imported", Value::Object(Map::new())));
            self.next_audit += 1;
            self.save(&e)?;
            self.next_seq += 1;
            self.entries.push(e);
            imported += 1;
        }
        let mut m = Map::new();
        m.push_unique("imported", Value::int(imported));
        m.push_unique("already_present", Value::int(present));
        m.push_unique("failed_integrity", Value::int(unverified));
        m.push_unique("no_room", Value::int(no_room));
        m.push_unique("external_action_executed", Value::Bool(false));
        Ok(Value::Object(m))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("tl-lib-{name}-{}", crate::util::random_hex(4)));
        let _ = fs::remove_dir_all(&d);
        d
    }

    fn response(p: f64) -> Value {
        loads(&format!(r#"{{"answers":{{"q":{{"type":"noul","probabilities":{{"false":{},"true":{p}}},"noul":{p}}}}},"model":"t","latency_ms":1.5}}"#, 1.0 - p), false).unwrap()
    }

    #[test]
    fn stores_verifies_and_reloads() {
        let dir = temp("a");
        let mut lib = Library::open(&dir).unwrap();
        let req = loads(r#"{"state":"x","questions":{"q":{"type":"noul","instructions":"ok?"}},"tags":["a"]}"#, false).unwrap();
        let id = lib.add(req.clone(), response(0.75), "First", "default").unwrap();
        let id2 = lib.add(req, response(0.25), "Second", "other").unwrap();
        assert_eq!(lib.decisions(Some("default")).len(), 1);
        assert_eq!(lib.decisions(None)[0].as_object().unwrap().get("id").unwrap().as_str(), Some(id2.as_str()));
        let rec = lib.get(&id, true).unwrap();
        assert_eq!(rec.get("integrity").unwrap().as_object().unwrap().get("verified"), Some(&Value::Bool(true)));
        assert_eq!(dumps(&lib.export_decision(&id, "lite").unwrap(), DumpOptions::COMPACT_SORTED).unwrap(), r#"{"questions":[{"answer":true,"question":"ok?"}],"tags":["a"],"title":"First"}"#);
        // corrections and training export
        let bad = loads(r#"{"true":0.5}"#, false).unwrap();
        assert_eq!(lib.feedback(Some(&Value::str(&id)), Some(&Value::str("q")), Some(&bad)).unwrap_err(), "Invalid target distribution");
        let t = loads(r#"{"false":1,"true":0}"#, false).unwrap();
        lib.feedback(Some(&Value::str(&id)), Some(&Value::str("q")), Some(&t)).unwrap();
        let tr = lib.training_records(Some("default"));
        let tr = tr.as_object().unwrap();
        assert_eq!(tr.get("corrections"), Some(&Value::int(1)));
        // reload from disk
        let again = Library::open(&dir).unwrap();
        assert_eq!(again.count(), 2);
        assert_eq!(again.stats().as_object().unwrap().get("feedback"), Some(&Value::int(1)));
        // a file changed outside the application is reported, not trusted
        let path = dir.join(format!("{id}.json"));
        let text = fs::read_to_string(&path).unwrap().replace("\"First\"", "\"Changed\"");
        fs::write(&path, text).unwrap();
        let changed = Library::open(&dir).unwrap();
        assert_eq!(changed.get(&id, true).unwrap_err(), "Decision integrity verification failed");
        fs::write(&path, "{broken").unwrap();
        let broken = Library::open(&dir).unwrap();
        let rec = broken.get(&id, false).unwrap();
        assert!(dumps(&Value::Object(rec), DumpOptions::REPLY).unwrap().contains("Stored record is unreadable"));
        // export -> import into an empty library
        let export = again.export();
        let mut fresh = Library::open(&temp("b")).unwrap();
        let r = fresh.import(&export, 200).unwrap();
        assert_eq!(r.as_object().unwrap().get("imported"), Some(&Value::int(2)));
        assert_eq!(fresh.get(&id, true).unwrap().get("title"), Some(&Value::str("First")));
        assert_eq!(fresh.import(&export, 200).unwrap().as_object().unwrap().get("already_present"), Some(&Value::int(2)));
        let mut lib = broken;
        let del = lib.delete_many(Some(&loads(&format!(r#"["{id}","{id2}","nope"]"#), false).unwrap())).unwrap();
        assert_eq!(del.as_object().unwrap().get("deleted"), Some(&Value::int(2)));
        assert_eq!(Library::open(&dir).unwrap().count(), 0);
    }

    /// A library exported by TypeLogic 1.x (SQLite store, tests/gen/mkexport.py) keeps its integrity here.
    #[test]
    fn imports_a_1x_export() {
        let export = loads(include_str!("../tests/v1_export.json"), false).unwrap();
        let mut lib = Library::open(&temp("v1")).unwrap();
        let r = lib.import(&export, 200).unwrap();
        assert_eq!(r.as_object().unwrap().get("imported"), Some(&Value::int(2)), "{}", dumps(&r, DumpOptions::REPLY).unwrap());
        assert_eq!(lib.stats().as_object().unwrap().get("feedback"), Some(&Value::int(1)));
        for d in lib.decisions(None) {
            assert_eq!(d.as_object().unwrap().get("integrity").unwrap().as_object().unwrap().get("verified"), Some(&Value::Bool(true)));
        }
        assert_eq!(lib.training_records(None).as_object().unwrap().get("corrections"), Some(&Value::int(1)));
    }
}
