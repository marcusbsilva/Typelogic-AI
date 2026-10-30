//! The local application: the web UI of 1.x and its API (port of `typelogic/server.py`), plus the machine API.
//!
//! UI API (`/api/...`, session token required, as in 1.x):
//!   GET  status, modes, needle, settings, templates, decisions?space=, decision?id=&format=, export, archive, tags,
//!        training-export?space=&all=, eval, evaluations, evaluation?name=&format=json|jsonl
//!   POST evaluate, delete, archive, settings, extract-evidence, templates, feedback, mode, extract, load, import,
//!        eval/start, eval/stop
//! Machine API (no token; loopback and same-origin checks only):
//!   POST /v1/decide[?engine=generalist|specialist|clef][&combine=ensemble]   a decision with the 1.x contract, not stored
//!   POST /v1/systemone?engine=specialist|clef             the raw System One API of a decision model
//!   GET  /api/health                                      version, loaded models and why they were chosen
use crate::archive;
use crate::bench;
use crate::casefmt;
use crate::engine::{ApiError, Engine, Engines};
use crate::evidence;
use crate::http::{self, Request, Response};
use crate::layout::Layout;
use crate::library::Library;
use crate::needle::{self, Needle};
use crate::router::Router;
use crate::templates;
use crate::titles;
use crate::util::{random_hex, write_atomic};
use std::path::PathBuf;
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;
use typelogic_core::json::{dumps, loads, DumpOptions, Map, Value};
use typelogic_core::schema;

/// Largest Score scale the llama.cpp System One API accepts.
pub const MAX_SCORE_LEVELS: usize = 10;
/// Largest UI request body (1.x: 8 MB).
const MAX_BODY: usize = 8 * 1024 * 1024;

/// What answers decisions: the models of the llama.cpp router, or the deterministic fixture of the UI tests.
pub trait Decide: Send + Sync {
    fn evaluate(&self, payload: &Value, force: Option<Engine>, combine: bool) -> Result<Value, ApiError>;
    fn loaded(&self) -> Vec<Engine>;
    /// A chat answer of the generalist, JSON constrained to `schema`.
    fn chat_json(&self, system: &str, user: &str, schema: &Value, max_tokens: u32) -> Result<String, ApiError>;
    fn count_tokens(&self, text: &str) -> Result<usize, ApiError>;
}

impl Decide for Engines {
    fn evaluate(&self, payload: &Value, force: Option<Engine>, combine: bool) -> Result<Value, ApiError> { Engines::evaluate(self, payload, force, combine) }
    fn loaded(&self) -> Vec<Engine> { self.loaded.clone() }
    fn chat_json(&self, system: &str, user: &str, schema: &Value, max_tokens: u32) -> Result<String, ApiError> { Engines::chat_json(self, system, user, schema, max_tokens) }
    fn count_tokens(&self, text: &str) -> Result<usize, ApiError> { Engines::count_tokens(self, text) }
}

/// The inference side: the router process, the engines and what is shown in Engine & privacy.
pub struct Inference {
    pub router: Option<Router>,
    pub engines: Option<Arc<dyn Decide>>,
    /// not_loaded | loading | ready | error
    pub status: &'static str,
    pub error: Option<String>,
    pub plan: String,
    /// llama.cpp device, None for the CPU
    pub device: Option<String>,
    /// (role, file)
    pub models: Vec<(String, String)>,
}

impl Inference {
    pub fn empty() -> Self { Self { router: None, engines: None, status: "not_loaded", error: None, plan: String::new(), device: None, models: Vec::new() } }
}

/// Settings saved in data/settings.json (Engine & privacy).
#[derive(Debug, Clone, PartialEq)]
pub struct Settings {
    /// auto (GPU when there is room) | cpu
    pub mode: String,
    /// auto | plumb | needle
    pub extract_backend: String,
    /// route | ensemble (questions with up to 16 options read by two models, product of experts)
    pub combine: String,
}

impl Settings {
    pub fn load(data: &std::path::Path) -> Settings {
        let stored = std::fs::read_to_string(data.join("settings.json")).ok().and_then(|t| loads(&t, false).ok());
        let get = |k: &str, allowed: &[&str], default: &str| {
            stored.as_ref().and_then(|v| v.as_object()?.get(k)?.as_str().map(str::to_owned)).filter(|v| allowed.contains(&v.as_str())).unwrap_or_else(|| default.into())
        };
        Settings {
            mode: get("mode", &["auto", "cpu"], "auto"),
            extract_backend: get("extract_backend", &["auto", "plumb", "needle"], "auto"),
            combine: get("combine", &["route", "ensemble"], "route"),
        }
    }

    fn save(&self, data: &std::path::Path) -> Result<(), String> {
        let mut m = Map::new();
        m.push_unique("mode", Value::str(&self.mode));
        m.push_unique("extract_backend", Value::str(&self.extract_backend));
        m.push_unique("combine", Value::str(&self.combine));
        let text = typelogic_core::json::dumps_indent(&Value::Object(m), 2, DumpOptions::REPLY).map_err(|e| e.message)?;
        write_atomic(&data.join("settings.json"), text.as_bytes()).map_err(|e| e.to_string())
    }
}

/// How the inference server is started (main.rs): returns the new inference state.
pub type Starter = dyn Fn(&Layout, bool) -> Inference + Send + Sync;

pub struct App {
    pub layout: Layout,
    pub data: PathBuf,
    pub token: String,
    pub version: &'static str,
    pub library: Mutex<Library>,
    pub settings: Mutex<Settings>,
    pub inference: RwLock<Inference>,
    pub starter: Box<Starter>,
    /// The running or last evaluation of the Evaluate page.
    pub evaluation: Mutex<Option<Arc<Mutex<bench::Job>>>>,
    /// Needle 3 field extraction (native engine, loaded on first use).
    pub needle: Needle,
}

/// A handler outcome: a value (status 200 or as given) or an error with its status and optional code.
struct Fail {
    status: u16,
    message: String,
    extra: Map,
}

fn fail(status: u16, message: impl Into<String>) -> Fail { Fail { status, message: message.into(), extra: Map::new() } }
fn invalid(message: impl Into<String>) -> Fail { fail(422, message) }
fn coded(status: u16, message: impl Into<String>, code: &str) -> Fail {
    let mut f = fail(status, message);
    f.extra.push_unique("code", Value::str(code));
    f
}

fn json(status: u16, value: &Value) -> Response {
    match dumps(value, DumpOptions::REPLY) {
        Ok(text) => Response::json(status, text),
        Err(e) => Response::error(500, &e.message),
    }
}

fn fail_response(f: Fail) -> Response {
    let mut m = Map::new();
    m.push_unique("error", Value::Str(f.message));
    for (k, v) in f.extra.into_entries() {
        m.push_unique(k, v);
    }
    json(f.status, &Value::Object(m))
}

fn obj(entries: &[(&str, Value)]) -> Value {
    let mut m = Map::new();
    for (k, v) in entries {
        m.push_unique(*k, v.clone());
    }
    Value::Object(m)
}

/// The /v1/systemone request for a decision model: the validated state (text, as 1.x renders it) and the questions
/// in the API's shape (Choice criteria always an object, empty descriptions as null).
pub fn systemone_request(payload: &Value, model: &str) -> Result<Value, String> {
    let validated = schema::validate(payload).map_err(|e| e.message)?;
    let mut questions = Map::with_capacity(validated.questions.len());
    for q in &validated.questions {
        let mut out = Map::with_capacity(3);
        out.push_unique("type", Value::str(&q.kind));
        out.push_unique("instructions", Value::str(&q.instr));
        match q.kind.as_str() {
            "choice" => {
                let mut criteria = Map::with_capacity(q.keys.len());
                if let Value::Object(m) = &q.criteria {
                    for (k, v) in m.iter() {
                        let text = v.as_str().unwrap_or("");
                        criteria.push_unique(k.clone(), if text.is_empty() { Value::Null } else { Value::str(text) });
                    }
                }
                out.push_unique("criteria", Value::Object(criteria));
            }
            "score" => {
                if q.options.len() > MAX_SCORE_LEVELS {
                    return Err(format!("{}: a Score question has at most {MAX_SCORE_LEVELS} levels", q.id));
                }
                out.push_unique("criteria", Value::Array(q.options.iter().map(Value::str).collect()));
            }
            _ => {
                if q.criteria.as_object().is_some_and(|m| !m.is_empty()) {
                    out.push_unique("criteria", q.criteria.clone());
                }
            }
        }
        questions.push_unique(q.id.clone(), Value::Object(out));
    }
    let mut body = Map::with_capacity(3);
    body.push_unique("model", Value::str(model));
    body.push_unique("state", Value::Str(validated.state));
    body.push_unique("questions", Value::Object(questions));
    Ok(Value::Object(body))
}

/// The JSON object body of a request (1.x rules: finite values only, an object, at most 8 MB).
fn body_object(request: &Request) -> Result<Map, Fail> {
    if request.body.is_empty() || request.body.len() > MAX_BODY {
        return Err(invalid("Invalid body or body exceeds 8 MB"));
    }
    let text = std::str::from_utf8(&request.body).map_err(|_| invalid("The body must be UTF-8 JSON"))?;
    match loads(text, true) {
        Ok(Value::Object(m)) => Ok(m),
        Ok(_) => Err(invalid("The body must be a JSON object")),
        Err(e) => Err(invalid(e.message)),
    }
}

fn label(mode: &str) -> &'static str { if mode == "cpu" { "CPU only" } else { "Automatic" } }

impl App {
    pub fn new(layout: Layout, data: PathBuf, version: &'static str, starter: Box<Starter>) -> Result<App, String> {
        let library = Library::open(&data.join("library")).map_err(|e| format!("library: {e}"))?;
        let settings = Settings::load(&data);
        let (layout_models, layout_config) = (layout.models_dir(), layout.config_dir());
        Ok(App {
            layout,
            data,
            token: random_hex(24),
            version,
            library: Mutex::new(library),
            settings: Mutex::new(settings),
            inference: RwLock::new(Inference::empty()),
            starter,
            evaluation: Mutex::new(None),
            needle: Needle::discover(&layout_models, &layout_config),
        })
    }

    fn lib(&self) -> std::sync::MutexGuard<'_, Library> { self.library.lock().unwrap_or_else(|e| e.into_inner()) }
    fn settings(&self) -> Settings { self.settings.lock().unwrap_or_else(|e| e.into_inner()).clone() }
    fn engines(&self) -> Option<Arc<dyn Decide>> {
        let inf = self.inference.read().unwrap_or_else(|e| e.into_inner());
        if inf.status == "ready" { inf.engines.clone() } else { None }
    }

    /// Load the models in the background (the UI is usable meanwhile). Returns false when a load already runs.
    pub fn load(self: &Arc<Self>) -> bool {
        {
            let mut inf = self.inference.write().unwrap_or_else(|e| e.into_inner());
            if inf.status == "loading" || inf.status == "ready" {
                return inf.status == "loading";
            }
            inf.status = "loading";
            inf.error = None;
            inf.router = None; // stops an old inference server first
            inf.engines = None;
        }
        let app = Arc::clone(self);
        std::thread::spawn(move || {
            let cpu = app.settings().mode == "cpu";
            let fresh = (app.starter)(&app.layout, cpu);
            *app.inference.write().unwrap_or_else(|e| e.into_inner()) = fresh;
        });
        true
    }

    pub fn handle(self: &Arc<Self>, request: Request) -> Response {
        let path = request.path.as_str();
        let method = request.method.as_str();
        match (method, path) {
            ("GET", "/" | "/index.html") => return self.page(),
            ("GET", "/app.js") => return static_file(crate::web::APP_JS, "text/javascript; charset=utf-8"),
            ("GET", "/style.css") => return static_file(crate::web::STYLE_CSS, "text/css; charset=utf-8"),
            ("GET", "/theme.js") => return static_file(crate::web::THEME_JS, "text/javascript; charset=utf-8"),
            ("GET", "/api/health") => return self.health(),
            ("POST", "/v1/decide") => return self.decide(&request),
            ("POST", "/v1/systemone") => return self.systemone(&request),
            _ => {}
        }
        if !path.starts_with("/api/") {
            return Response::error(404, "File not found");
        }
        if !self.authorized(&request) {
            return Response::error(401, "Unauthorized session");
        }
        if method == "GET" && path == "/api/evaluation" {
            let name = request.query_text("name").unwrap_or_default();
            return match bench::read(&self.data, &name, request.query_text("format").as_deref() == Some("jsonl")) {
                Ok((text, kind)) => Response { status: 200, content_type: kind, body: text.into_bytes() },
                Err(e) => Response::error(404, &e),
            };
        }
        let outcome = match method {
            "GET" => self.get(&request),
            "POST" => body_object(&request).and_then(|body| self.post(&request, body)),
            _ => Err(fail(405, "method not allowed")),
        };
        match outcome {
            Ok((status, v)) => json(status, &v),
            Err(f) => fail_response(f),
        }
    }

    fn authorized(&self, request: &Request) -> bool {
        let token = request
            .header("authorization")
            .and_then(|a| a.strip_prefix("Bearer "))
            .or_else(|| request.header("x-typelogic-token"))
            .unwrap_or("");
        let (a, b) = (token.as_bytes(), self.token.as_bytes());
        a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
    }

    fn page(&self) -> Response {
        let html = crate::web::INDEX_HTML.replace("__SESSION_TOKEN__", &self.token).replace("__VERSION__", self.version);
        Response::html(html)
    }

    // ------------------------------------------------------------------------------------------------ GET

    fn get(self: &Arc<Self>, request: &Request) -> Result<(u16, Value), Fail> {
        let space = request.query_text("space").unwrap_or_else(|| "default".into());
        let v = match request.path.as_str() {
            "/api/status" => obj(&[
                ("engine", self.engine_health()),
                ("mode", Value::Str(self.settings().mode)),
                ("restarting", Value::Bool(false)),
                ("stats", self.lib().stats()),
                ("library_limit", Value::int(archive::limit() as i64)),
            ]),
            "/api/modes" => self.modes(),
            "/api/eval" => match self.evaluation.lock().unwrap_or_else(|e| e.into_inner()).clone() {
                Some(job) => job.lock().unwrap_or_else(|e| e.into_inner()).to_value(true),
                None => obj(&[("status", Value::str("idle"))]),
            },
            "/api/evaluations" => bench::list(&self.data),
            "/api/needle" => self.needle.status(),
            "/api/settings" => obj(&[("extract_backend", Value::Str(self.settings().extract_backend))]),
            "/api/templates" => templates::load_all(&self.data),
            "/api/decisions" => obj(&[("decisions", Value::Array(self.lib().decisions(Some(&space))))]),
            "/api/decision" => {
                let id = request.query_text("id").unwrap_or_default();
                let format = request.query_text("format").unwrap_or_else(|| "full".into());
                self.lib().export_decision(&id, &format).map_err(invalid)?
            }
            "/api/export" => self.lib().export(),
            "/api/archive" => archive::info(&self.lib(), &self.data),
            "/api/tags" => obj(&[
                ("suggested", Value::Array(casefmt::TAGS.iter().map(|t| Value::str(*t)).collect())),
                ("max_tags", Value::int(casefmt::MAX_TAGS as i64)),
                ("max_chars", Value::int(casefmt::MAX_TAG_CHARS as i64)),
            ]),
            "/api/training-export" => {
                let all = request.query_text("all").as_deref() == Some("1");
                self.lib().training_records(if all { None } else { Some(&space) })
            }
            _ => return Err(fail(404, "Route not found")),
        };
        Ok((200, v))
    }

    fn engine_health(&self) -> Value {
        let inf = self.inference.read().unwrap_or_else(|e| e.into_inner());
        let models = inf
            .models
            .iter()
            .map(|(role, file)| obj(&[("role", Value::str(role)), ("file", Value::str(file))]))
            .collect();
        let engines = inf.engines.as_ref().map(|e| e.loaded().into_iter().map(|x| Value::str(x.name())).collect()).unwrap_or_default();
        obj(&[
            ("status", Value::str(inf.status)),
            ("error", inf.error.clone().map(Value::Str).unwrap_or(Value::Null)),
            ("model", Value::str("typelogic")),
            ("version", Value::str(self.version)),
            ("runtime", Value::str("llama.cpp v0.8.0")),
            ("engines", Value::Array(engines)),
            ("models", Value::Array(models)),
            ("plan", Value::Str(inf.plan.clone())),
            ("device", inf.device.clone().map(Value::Str).unwrap_or(Value::str("CPU"))),
            ("routing", obj(&[("max_generalist_options", Value::int(crate::direct::MAX_OPTIONS as i64))])),
            ("core", obj(&[("backend", Value::str("rust")), ("core_version", Value::str(typelogic_core::VERSION))])),
            ("offline", Value::Bool(true)),
        ])
    }

    fn modes(&self) -> Value {
        let inf = self.inference.read().unwrap_or_else(|e| e.into_inner());
        let mode = self.settings().mode;
        obj(&[
            ("mode", Value::str(&mode)),
            ("label", Value::str(label(&mode))),
            ("restarting", Value::Bool(false)),
            ("engine_status", Value::str(inf.status)),
            ("plan", Value::Str(inf.plan.clone())),
            ("settings", obj(&[("combine", Value::Str(self.settings().combine))])),
            (
                "availability",
                obj(&[
                    ("auto", obj(&[("available", Value::Bool(true)), ("detail", Value::str("The models that fit the GPU; the CPU when there is none"))])),
                    ("cpu", obj(&[("available", Value::Bool(true)), ("detail", Value::str("Specialist + Plumb on the processor; no GPU memory used"))])),
                ]),
            ),
        ])
    }

    // ------------------------------------------------------------------------------------------------ POST

    fn post(self: &Arc<Self>, request: &Request, body: Map) -> Result<(u16, Value), Fail> {
        let v = match request.path.as_str() {
            "/api/evaluate" => return self.evaluate(body).map(|v| (200, v)),
            "/api/delete" => {
                let mut lib = self.lib();
                if body.contains_key("ids") { lib.delete_many(body.get("ids")) } else { lib.delete(body.get("id")) }.map_err(invalid)?
            }
            "/api/archive" => {
                if body.get("confirm") != Some(&Value::Bool(true)) {
                    return Err(invalid("Send {\"confirm\": true} to archive every decision of the library"));
                }
                let mut lib = self.lib();
                archive::archive(&mut lib, &self.data, self.version).map_err(|e| if e.contains("empty") { invalid(e) } else { coded(500, e, "archive_failed") })?
            }
            "/api/settings" => {
                let Some(b) = body.get("extract_backend") else { return Err(invalid("Nothing to change (supported: extract_backend)")) };
                let b = b.as_str().filter(|b| ["auto", "plumb", "needle"].contains(b)).ok_or_else(|| invalid("extract_backend must be one of: auto, plumb, needle"))?;
                let mut s = self.settings.lock().unwrap_or_else(|e| e.into_inner());
                s.extract_backend = b.into();
                s.save(&self.data).map_err(|e| fail(500, e))?;
                obj(&[("extract_backend", Value::str(b))])
            }
            "/api/extract-evidence" => self.extract_evidence(&body)?,
            "/api/templates" => templates::save(body.get("template"), &self.data, body.get("overwrite") == Some(&Value::Bool(true))).map_err(invalid)?,
            "/api/feedback" => self.lib().feedback(body.get("id"), body.get("question_id"), body.get("target")).map_err(invalid)?,
            "/api/mode" => return self.switch_mode(&body),
            "/api/eval/start" => return self.start_evaluation(&body),
            "/api/eval/stop" => match self.evaluation.lock().unwrap_or_else(|e| e.into_inner()).clone() {
                Some(job) => {
                    let j = job.lock().unwrap_or_else(|e| e.into_inner());
                    j.cancel.store(true, std::sync::atomic::Ordering::Relaxed);
                    obj(&[("id", Value::Str(j.id.clone())), ("stopping", Value::Bool(j.status == "running"))])
                }
                None => return Err(invalid("No evaluation is running")),
            },
            "/api/extract" => {
                if !self.needle.available() {
                    let status = self.needle.status();
                    return Err(coded(503, status.as_object().and_then(|m| m.get("reason")).and_then(Value::as_str).unwrap_or("Needle is not available"), "needle_unavailable"));
                }
                match self.needle.extract(body.get("text"), body.get("fields")) {
                    Ok(v) => v,
                    Err(needle::Extract::Invalid(e)) => return Err(invalid(e)),
                    Err(needle::Extract::Engine(e)) => return Err(coded(503, e, "needle_unavailable")),
                }
            }
            "/api/load" => {
                let loading = self.load();
                return Ok((202, obj(&[("loading", Value::Bool(loading)), ("status", Value::str(self.inference.read().unwrap_or_else(|e| e.into_inner()).status))])));
            }
            "/api/import" => {
                let export = body.get("export").ok_or_else(|| invalid("Send {\"export\": <the content of typelogic-export.json>}"))?;
                self.lib().import(export, archive::limit()).map_err(invalid)?
            }
            _ => return Err(fail(404, "Route not found")),
        };
        Ok((200, v))
    }

    /// POST /api/eval/start {source: jevbench|file, tiers?, jsonl?, limit?, engine?, combine?, probes?}
    fn start_evaluation(self: &Arc<Self>, body: &Map) -> Result<(u16, Value), Fail> {
        let Some(engines) = self.engines() else {
            return Err(coded(503, "Engine is not ready. Wait for loading or open Engine & privacy.", "engine_not_ready"));
        };
        let source = body.get("source").and_then(Value::as_str).unwrap_or("jevbench");
        let mut items = match source {
            "jevbench" => {
                let tiers: Vec<String> = match body.get("tiers") {
                    None | Some(Value::Null) => Vec::new(),
                    Some(Value::Array(a)) => a.iter().filter_map(Value::as_str).map(str::to_owned).collect(),
                    _ => return Err(invalid("tiers must be a list of original, easy, hard")),
                };
                if tiers.iter().any(|t| !["original", "easy", "hard"].contains(&t.as_str())) {
                    return Err(invalid("tiers must be a list of original, easy, hard"));
                }
                bench::jevbench(&tiers)
            }
            "file" => {
                let text = body.get("jsonl").and_then(Value::as_str).ok_or_else(|| invalid("Send the file content as jsonl"))?;
                bench::parse_jsonl(text, "file").map_err(|errors| {
                    let mut f = invalid(format!("The file has problems: {}", errors.join("; ")));
                    f.extra.push_unique("errors", Value::Array(errors.into_iter().map(Value::Str).collect()));
                    f
                })?
            }
            _ => return Err(invalid("source must be jevbench or file")),
        };
        match body.get("limit") {
            None | Some(Value::Null) => {}
            Some(Value::Int(n)) => match n.parse::<usize>() {
                Ok(n) if n > 0 => items.truncate(n),
                _ => return Err(invalid("limit must be a positive integer")),
            },
            _ => return Err(invalid("limit must be a positive integer")),
        }
        if items.is_empty() {
            return Err(invalid("Nothing to evaluate"));
        }
        let engine = match body.get("engine").and_then(Value::as_str) {
            None | Some("" | "auto") => None,
            Some(e) => Some(Engine::parse(e).ok_or_else(|| invalid("engine must be auto, generalist, specialist or clef"))?),
        };
        if let Some(e) = engine {
            if !engines.loaded().contains(&e) {
                return Err(invalid(format!("The {} model is not loaded in this mode", e.name())));
            }
        }
        let combine = match body.get("combine").and_then(Value::as_str) {
            None | Some("" | "route") => false,
            Some("ensemble") => true,
            Some(_) => return Err(invalid("combine must be route or ensemble")),
        };
        if combine && engine.is_some() {
            return Err(invalid("Combine applies to automatic routing; choose the engine Automatic"));
        }
        let probes = body.get("probes") != Some(&Value::Bool(false));
        let config = bench::Config { source: source.to_owned(), engine, combine, probes };
        let job = {
            let mut slot = self.evaluation.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(old) = slot.as_ref() {
                if old.lock().unwrap_or_else(|e| e.into_inner()).status == "running" {
                    return Err(coded(409, "An evaluation is already running; stop it first", "evaluation_running"));
                }
            }
            let job = Arc::new(Mutex::new(bench::new_job(&config, items.len())));
            *slot = Some(Arc::clone(&job));
            job
        };
        let data = self.data.clone();
        let reply = job.lock().unwrap_or_else(|e| e.into_inner()).to_value(false);
        std::thread::spawn(move || bench::run(&job, items, engines, config, &data));
        Ok((202, reply))
    }

    fn switch_mode(self: &Arc<Self>, body: &Map) -> Result<(u16, Value), Fail> {
        let mode = body.get("mode").and_then(Value::as_str).map(|m| m.trim().to_lowercase()).unwrap_or_default();
        if mode != "auto" && mode != "cpu" {
            return Err(invalid("mode must be auto or cpu"));
        }
        let combine = match body.get("combine") {
            None | Some(Value::Null) => None,
            Some(Value::Str(c)) if c == "route" || c == "ensemble" => Some(c.clone()),
            _ => return Err(invalid("combine must be route or ensemble")),
        };
        if let Some(c) = &combine {
            let mut s = self.settings.lock().unwrap_or_else(|e| e.into_inner());
            s.combine = c.clone();
            s.save(&self.data).map_err(|e| fail(500, e))?;
        }
        let status = self.inference.read().unwrap_or_else(|e| e.into_inner()).status;
        if self.settings().mode == mode && (status == "ready" || (status == "loading" && combine.is_some())) {
            // same mode: only the combination changed, the models stay as they are
            return Ok((202, obj(&[("mode", Value::Str(mode)), ("restarting", Value::Bool(false)), ("loading", Value::Bool(false)), ("note", Value::Null)])));
        }
        if self.inference.read().unwrap_or_else(|e| e.into_inner()).status == "loading" {
            return Err(coded(409, "The engine is still loading; wait until it is ready or has failed, then change the mode", "mode_unavailable"));
        }
        {
            let mut s = self.settings.lock().unwrap_or_else(|e| e.into_inner());
            s.mode = mode.clone();
            s.save(&self.data).map_err(|e| fail(500, e))?;
        }
        {
            let mut inf = self.inference.write().unwrap_or_else(|e| e.into_inner());
            inf.status = "not_loaded";
        }
        self.load();
        Ok((202, obj(&[("mode", Value::Str(mode)), ("restarting", Value::Bool(false)), ("loading", Value::Bool(true)), ("note", Value::Null)])))
    }

    /// POST /api/evaluate: the 1.x flow (`Application.evaluate`): validate, answer, title, store.
    fn evaluate(&self, body: Map) -> Result<Value, Fail> {
        let Some(engines) = self.engines() else {
            return Err(coded(503, "Engine is not ready. Wait for loading or open Engine & privacy.", "engine_not_ready"));
        };
        let title = casefmt::validate_title(body.get("title")).map_err(invalid)?;
        let tags = casefmt::validate_tags(body.get("tags")).map_err(invalid)?;
        let (mut payload, case_info) = if casefmt::is_v2(&Value::Object(body.clone())) {
            let (mut p, info) = casefmt::normalize(&body).map_err(invalid)?;
            // the case format drops unknown keys; Needle fields stay attached so that every answer is flagged
            if let Some(record) = body.get("extracted_fields").filter(|v| !matches!(v, Value::Null)) {
                p.push_unique("extracted_fields", record.clone());
            }
            (p, Some(info))
        } else {
            (body, None)
        };
        payload = payload.into_entries().into_iter().filter(|(k, _)| !["review_mode", "title", "tags"].contains(&k.as_str())).fold(Map::new(), |mut m, (k, v)| {
            m.push_unique(k, v);
            m
        });
        schema::validate(&Value::Object(payload.clone())).map_err(|e| invalid(e.message))?;
        let space = match payload.get("space") {
            None => "default".to_owned(),
            Some(Value::Str(s)) if (1..=100).contains(&typelogic_core::text::py_len(s)) => s.clone(),
            _ => return Err(invalid("Invalid workspace")),
        };
        let full = {
            let lib = self.lib();
            archive::full_message(&lib, &self.data).map(|m| (m, archive::info(&lib, &self.data)))
        };
        if let Some((message, info)) = full {
            let mut f = coded(409, message, "library_full");
            f.extra.push_unique("archive", info);
            return Err(f);
        }
        // fields extracted by Needle: data next to the case, unverified, every answer flagged (1.x server.evaluate)
        let extracted = match payload.get("extracted_fields") {
            None | Some(Value::Null) => None,
            Some(record) => {
                let record = needle::check_attached(record).map_err(invalid)?;
                let mut state = Map::new();
                state.push_unique("provided_context", payload.get("state").cloned().unwrap_or(Value::Null));
                state.push_unique("model_extracted_fields", record.clone());
                payload.insert("state", Value::Object(state));
                payload.insert("extracted_fields", record.clone());
                Some(record)
            }
        };
        let combine = self.settings().combine == "ensemble";
        let response = engines.evaluate(&Value::Object(payload.clone()), None, combine).map_err(|e| match e.status {
            400 | 503 => invalid(e.message),
            _ => fail(500, format!("Processing failed: {}", e.message)),
        })?;
        let mut response = response.as_object().cloned().unwrap_or_default();
        // As in Jev: confidence is the probability of the most likely answer (a model estimate, not accuracy).
        if let Some(Value::Object(answers)) = response.get("answers").cloned() {
            let mut out = Map::new();
            for (k, a) in answers.into_entries() {
                let mut a = a.as_object().cloned().unwrap_or_default();
                let top = a.get("probabilities").and_then(Value::as_object).map(|p| p.iter().filter_map(|(_, v)| v.to_f64().ok()).fold(f64::NEG_INFINITY, f64::max));
                if let Some(top) = top.filter(|t| t.is_finite()) {
                    a.insert("confidence", Value::Float(top));
                }
                out.push_unique(k, Value::Object(a));
            }
            response.insert("answers", Value::Object(out));
        }
        if let Some(record) = &extracted {
            response.insert("extracted_fields", record.clone());
        }
        if let Some(info) = &case_info {
            casefmt::apply_to_response(&mut response, info);
        }
        if extracted.is_some() {
            if let Some(Value::Object(answers)) = response.get("answers").cloned() {
                let mut out = Map::new();
                for (k, a) in answers.into_entries() {
                    let mut a = a.as_object().cloned().unwrap_or_default();
                    let mut reasons = a.get("review_reasons").and_then(Value::as_array).cloned().unwrap_or_default();
                    reasons.push(Value::str("model_extracted_fields"));
                    a.insert("review_reasons", Value::Array(reasons));
                    a.insert("recommendation", Value::str("review"));
                    out.push_unique(k, Value::Object(a));
                }
                response.insert("answers", Value::Object(out));
            }
        }
        // Without a title from the user, a short one is written from the case now that the evaluation is done.
        let (title, source) = match title {
            Some(t) => (t, "user".to_owned()),
            None => {
                let state = payload.get("state").cloned().unwrap_or(Value::Null);
                let state = if extracted.is_some() { state.as_object().and_then(|m| m.get("provided_context")).cloned().unwrap_or(state) } else { state };
                let text = case_info.as_ref().map(|i| Value::str(&i.case)).unwrap_or(state);
                self.title_for(engines.as_ref(), &text)
            }
        };
        payload.insert("title", Value::str(&title));
        if !tags.is_empty() {
            payload.insert("tags", Value::Array(tags.iter().map(Value::str).collect()));
        }
        response.insert("title_source", Value::Str(source));
        let mut lib = self.lib();
        let id = lib.add(Value::Object(payload), Value::Object(response.clone()), &title, &space).map_err(|e| fail(500, e))?;
        let record = lib.get(&id, true).map_err(|e| fail(500, e))?;
        response.insert("decision_id", Value::Str(id));
        response.insert("title", Value::Str(title));
        response.insert("tags", Value::Array(tags.into_iter().map(Value::Str).collect()));
        response.insert("created", record.get("created").cloned().unwrap_or(Value::Null));
        response.insert("integrity", record.get("integrity").cloned().unwrap_or(Value::Null));
        Ok(Value::Object(response))
    }

    /// (title, source): the generalist's short title when it is loaded and its answer is usable, else the case text.
    fn title_for(&self, engines: &dyn Decide, text: &Value) -> (String, String) {
        if engines.loaded().contains(&Engine::Generalist) {
            let case = match text {
                Value::Str(s) => s.clone(),
                other => dumps(other, DumpOptions::REPLY).unwrap_or_default(),
            };
            let cut: String = case.chars().take(6000).collect();
            let schema = loads(r#"{"type":"object","properties":{"title":{"type":"string"}},"required":["title"]}"#, false).expect("schema");
            if let Some(t) = engines.chat_json(titles::SYSTEM, &cut, &schema, 40).ok().and_then(|c| titles::from_model(&c)) {
                return (t, "model:plumb-4b".into());
            }
        }
        (titles::from_text(text), "case text".into())
    }

    /// POST /api/extract-evidence: the evidence schema filled by the generalist (`evidence.extract`).
    fn extract_evidence(&self, body: &Map) -> Result<Value, Fail> {
        let (text, schema) = evidence::validate_request(body.get("text"), body.get("schema")).map_err(invalid)?;
        let backend = body.get("backend").and_then(Value::as_str).map(str::to_owned).unwrap_or_else(|| self.settings().extract_backend);
        if !["auto", "plumb", "needle"].contains(&backend.as_str()) {
            return Err(invalid("backend must be auto, plumb or needle"));
        }
        let engines = self.engines().filter(|e| e.loaded().contains(&Engine::Generalist));
        // auto: Plumb when it is loaded, otherwise Needle for a short text (1.x: Ollama, otherwise Needle)
        let use_needle = backend == "needle" || (backend == "auto" && engines.is_none() && self.needle.available());
        if use_needle {
            if !self.needle.available() {
                let status = self.needle.status();
                return Err(coded(503, status.as_object().and_then(|m| m.get("reason")).and_then(Value::as_str).unwrap_or("Needle is not available"), "extractor_unavailable"));
            }
            let started = std::time::Instant::now();
            let (values, confidence) = self.needle.evidence_values(&text, &schema).map_err(|e| match e {
                needle::Extract::Invalid(m) => invalid(m),
                needle::Extract::Engine(m) => coded(503, m, "extractor_unavailable"),
            })?;
            return Ok(obj(&[
                ("backend", Value::str("needle")),
                ("model", Value::str("needle3")),
                ("confidence", confidence),
                ("fields", evidence::fields(&values, &schema, &text)),
                ("chars", Value::int(typelogic_core::text::py_len(&text) as i64)),
                ("seconds", Value::Float((started.elapsed().as_secs_f64() * 1000.0).round() / 1000.0)),
                ("note", Value::str(evidence::NOTE)),
            ]));
        }
        let Some(engines) = engines else {
            return Err(coded(503, "The generalist model (Plumb) is not loaded; evidence extraction needs it (or Needle for a short text)", "extractor_unavailable"));
        };
        let started = std::time::Instant::now();
        let prompt = evidence::prompt(&text, &schema);
        let tokens = engines.count_tokens(&prompt).map_err(|e| fail(500, e.message))?;
        if tokens + 600 + 64 * schema.len() > evidence::MAX_CONTEXT {
            return Err(invalid("The text is too long for evidence extraction; extract from a shorter part (it is never cut automatically)"));
        }
        let answer = engines
            .chat_json(evidence::SYSTEM, &prompt, &evidence::json_schema(&schema), (64 * schema.len() as u32).max(256))
            .map_err(|e| coded(503, e.message, "extractor_unavailable"))?;
        let values = match loads(&answer, false) {
            Ok(Value::Object(m)) => m,
            _ => return Err(coded(503, "The model did not return a JSON object", "extractor_unavailable")),
        };
        Ok(obj(&[
            ("backend", Value::str("plumb")),
            ("model", Value::str("plumb-4b-v5-Q4_K_M")),
            ("prompt_tokens", Value::int(tokens as i64)),
            ("fields", evidence::fields(&values, &schema, &text)),
            ("chars", Value::int(typelogic_core::text::py_len(&text) as i64)),
            ("seconds", Value::Float((started.elapsed().as_secs_f64() * 1000.0).round() / 1000.0)),
            ("note", Value::str(evidence::NOTE)),
        ]))
    }

    // ------------------------------------------------------------------------------------------------ machine API

    fn health(&self) -> Response {
        let inf = self.inference.read().unwrap_or_else(|e| e.into_inner());
        let engines: Vec<Value> = inf.engines.as_ref().map(|e| e.loaded().into_iter().map(|x| Value::str(x.name())).collect()).unwrap_or_default();
        let models = match inf.router.as_ref() {
            Some(r) => match http::call(r.addr, "GET", "/models", None, Duration::from_secs(10)) {
                Ok((200, body)) => loads(&String::from_utf8_lossy(&body), true).unwrap_or(Value::Null),
                _ => Value::Null,
            },
            None => Value::Null,
        };
        json(
            200,
            &obj(&[
                ("app", Value::str("TypeLogic")),
                ("version", Value::str(self.version)),
                ("core", Value::str(typelogic_core::VERSION)),
                ("offline", Value::Bool(true)),
                ("inference", Value::str(if inf.status == "ready" { "ready" } else { inf.status })),
                ("engines", Value::Array(engines)),
                ("plan", Value::Str(inf.plan.clone())),
                ("models", models),
            ]),
        )
    }

    fn decide(&self, request: &Request) -> Response {
        let payload = match std::str::from_utf8(&request.body).ok().and_then(|t| loads(t, true).ok()) {
            Some(v) => v,
            None => return Response::error(400, "the body must be UTF-8 JSON"),
        };
        let force = match request.query_param("engine") {
            None | Some("" | "auto") => None,
            Some(e) => match Engine::parse(e) {
                Some(e) => Some(e),
                None => return Response::error(400, "engine must be auto, generalist, specialist or clef"),
            },
        };
        let combine = match request.query_param("combine") {
            None | Some("" | "route") => false,
            Some("ensemble") => true,
            Some(_) => return Response::error(400, "combine must be route or ensemble"),
        };
        let Some(engines) = self.engines() else { return Response::error(503, "the inference server is not ready") };
        match engines.evaluate(&payload, force, combine) {
            Ok(v) => json(200, &v),
            Err(e) => Response::error(e.status, &e.message),
        }
    }

    fn systemone(&self, request: &Request) -> Response {
        let payload = match std::str::from_utf8(&request.body).ok().and_then(|t| loads(t, true).ok()) {
            Some(v) => v,
            None => return Response::error(400, "the body must be UTF-8 JSON"),
        };
        let engine = request.query_param("engine").unwrap_or("specialist");
        if !matches!(engine, "specialist" | "clef") {
            return Response::error(400, "engine must be specialist or clef");
        }
        let body = match systemone_request(&payload, engine).and_then(|v| dumps(&v, DumpOptions::REPLY).map_err(|e| e.message)) {
            Ok(b) => b,
            Err(message) => return Response::error(400, &message),
        };
        let addr = self.inference.read().unwrap_or_else(|e| e.into_inner()).router.as_ref().map(|r| r.addr);
        let Some(addr) = addr else { return Response::error(503, "the inference server is not running") };
        match http::call(addr, "POST", "/v1/systemone", Some(body.as_bytes()), Duration::from_secs(900)) {
            Ok((status, data)) => Response { status, content_type: "application/json; charset=utf-8", body: data },
            Err(e) => Response::error(502, &format!("inference server: {e}")),
        }
    }
}

fn static_file(data: &'static str, kind: &'static str) -> Response { Response { status: 200, content_type: kind, body: data.as_bytes().to_vec() } }

// ------------------------------------------------------------------------------------------------ fixture engine

/// The deterministic engine of the UI tests (`web/tests/fixture_server.py` of 1.x): the first alternative gets 0.9.
#[cfg(feature = "fixture")]
pub struct Fixture;

#[cfg(feature = "fixture")]
impl Decide for Fixture {
    fn evaluate(&self, payload: &Value, _force: Option<Engine>, _combine: bool) -> Result<Value, ApiError> {
        let v = schema::validate(payload).map_err(|e| ApiError { status: 400, message: e.message })?;
        let rows: Vec<Vec<f64>> = v.questions.iter().map(|q| {
            let n = q.keys.len();
            (0..n).map(|i| if i == 0 { 0.9 } else { 0.1 / (n - 1) as f64 }).collect()
        }).collect();
        let answers = schema::answers(&v.questions, &rows, v.threshold, &Value::str("unvalidated")).map_err(|e| ApiError { status: 500, message: e.message })?;
        Ok(obj(&[("answers", Value::Object(answers)), ("model", Value::str("fixture")), ("latency_ms", Value::Float(12.5)), ("external_action_executed", Value::Bool(false))]))
    }
    fn loaded(&self) -> Vec<Engine> { vec![Engine::Generalist, Engine::Specialist] }
    fn chat_json(&self, system: &str, _user: &str, _schema: &Value, _max: u32) -> Result<String, ApiError> {
        if system == evidence::SYSTEM {
            return Ok(r#"{"payment_related": true, "payout_failed_days": 3, "urgency": "high"}"#.into());
        }
        Err(ApiError { status: 503, message: "fixture".into() })
    }
    fn count_tokens(&self, text: &str) -> Result<usize, ApiError> { Ok(text.len() / 4) }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(json: &str) -> Result<String, String> {
        let v = systemone_request(&loads(json, true).unwrap(), "specialist")?;
        Ok(dumps(&v, DumpOptions::COMPACT_SORTED).unwrap())
    }

    #[test]
    fn normalizes_questions_for_the_api() {
        let out = request(r#"{"state":"Order 17 arrived damaged.","questions":{
            "route":{"type":"choice","instructions":"Which team?","criteria":["billing","shipping"]},
            "angry":{"type":"noul","instructions":"Is the customer angry?"},
            "urgency":{"type":"score","instructions":"How urgent?","criteria":["low","high"]}}}"#).unwrap();
        assert!(out.contains(r#""route":{"criteria":{"billing":null,"shipping":null},"instructions":"Which team?","type":"choice"}"#), "{out}");
        assert!(out.contains(r#""angry":{"instructions":"Is the customer angry?","type":"noul"}"#), "{out}");
        assert!(out.contains(r#""urgency":{"criteria":["low","high"]"#), "{out}");
        assert!(out.contains(r#""model":"specialist""#) && out.contains(r#""state":"Order 17 arrived damaged.""#));
    }

    #[test]
    fn keeps_the_1x_rules_and_the_api_limits() {
        assert_eq!(request(r#"{"state":"x","questions":{}}"#).unwrap_err(), "Provide 1–32 questions");
        let levels: Vec<String> = (0..11).map(|i| format!("\"level {i}\"")).collect();
        let eleven = format!(r#"{{"state":"x","questions":{{"s":{{"type":"score","instructions":"i","criteria":[{}]}}}}}}"#, levels.join(","));
        assert!(request(&eleven).unwrap_err().contains("at most 10 levels"));
    }

    #[test]
    fn structured_state_is_sent_as_its_canonical_text() {
        let out = request(r#"{"state":{"b":1,"a":"x"},"questions":{"q":{"type":"noul","instructions":"ok?"}}}"#).unwrap();
        assert!(out.contains(r#""state":"{\"a\": \"x\", \"b\": 1}""#), "{out}");
    }

    #[test]
    fn settings_round_trip() {
        let dir = std::env::temp_dir().join(format!("tl-settings-{}", random_hex(4)));
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(Settings::load(&dir), Settings { mode: "auto".into(), extract_backend: "auto".into(), combine: "route".into() });
        let s = Settings { mode: "cpu".into(), extract_backend: "plumb".into(), combine: "ensemble".into() };
        s.save(&dir).unwrap();
        assert_eq!(Settings::load(&dir), s);
        std::fs::write(dir.join("settings.json"), r#"{"mode":"hybrid"}"#).unwrap();
        assert_eq!(Settings::load(&dir).mode, "auto");
        std::fs::remove_dir_all(dir).unwrap();
    }
}
