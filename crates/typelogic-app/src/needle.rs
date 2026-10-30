//! Field extraction with Needle 3 (Cactus Compute, Apache-2.0, https://github.com/cactus-compute/needle), offline.
//!
//! Port of `typelogic/needle.py` of 1.x. Only the native engine (libneedle3.dll / .so / .dylib) and the weights
//! (needle3.cact) are used, from models/needle/, after their SHA-256 hashes are checked against the `needle` entry of
//! config/models.lock.json. The `cactus-needle` Python package is never used (it sends usage telemetry and contacts
//! Hugging Face); the native engine has no network code. The C signatures follow needle/__init__.py of
//! cactus-compute/needle (Apache License 2.0); the rest is TypeLogic code.
//!
//! The engine keeps one global state per process, so every call goes through one lock. Extracted values are never
//! trusted: each one is checked against the text, and a decision that carries them always requires review.
use crate::casefmt;
use crate::evidence;
use crate::setup::sha256_file;
use crate::util::{civil, today_local};
use std::ffi::{c_char, c_float, c_int, c_void, CString};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Instant;
use typelogic_core::json::{dumps, loads, DumpOptions, Map, Value};
use typelogic_core::text::{py_len, strip};

pub const WEIGHTS: &str = "needle3.cact";
pub const MAX_TEXT_CHARS: usize = 20000;
pub const MAX_FIELDS: usize = 12;
const MAX_OPTIONS: usize = 20;
const MAX_NEW_TOKENS: c_int = 512;
pub const FIELD_TYPES: [&str; 5] = ["text", "number", "integer", "date", "boolean"];
const TOOL_NAME: &str = "record_fields";
const TOOL_DESCRIPTION: &str = "Record the requested fields exactly as stated in the text. Leave out any field the text does not state.";
pub const NOTE: &str = "Extracted by a small local model (Needle 3). The values are unverified: the scenario text is authoritative, and a decision that includes them requires review.";
/// Evidence extraction uses Needle for short texts only: on long documents it misses or misreads most fields.
pub const SHORT_TEXT_CHARS: usize = 1500;

#[cfg(windows)]
pub const LIBRARY: &str = "libneedle3.dll";
#[cfg(target_os = "macos")]
pub const LIBRARY: &str = "libneedle3.dylib";
#[cfg(all(unix, not(target_os = "macos")))]
pub const LIBRARY: &str = "libneedle3.so";

/// Wheel platform tag of this computer (the `platforms` keys of the lock entry).
pub fn platform() -> Option<&'static str> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("windows", "x86_64") => Some("win_amd64"),
        ("windows", "aarch64") => Some("win_arm64"),
        ("linux", "x86_64") => Some(if cfg!(target_env = "musl") { "musllinux_1_2_x86_64" } else { "manylinux2014_x86_64" }),
        ("linux", "aarch64") => Some(if cfg!(target_env = "musl") { "musllinux_1_2_aarch64" } else { "manylinux2014_aarch64" }),
        ("macos", "aarch64") => Some("macosx_11_0_arm64"),
        ("macos", "x86_64") => Some("macosx_11_0_x86_64"),
        _ => None,
    }
}

pub fn presets() -> Value {
    let field = |name: &str, kind: &str, description: &str| {
        let mut m = Map::new();
        m.push_unique("name", Value::str(name));
        m.push_unique("type", Value::str(kind));
        m.push_unique("description", Value::str(description));
        Value::Object(m)
    };
    let preset = |label: &str, fields: Vec<Value>| {
        let mut m = Map::new();
        m.push_unique("label", Value::str(label));
        m.push_unique("fields", Value::Array(fields));
        Value::Object(m)
    };
    let mut m = Map::new();
    m.push_unique(
        "request",
        preset(
            "Trusted record vs received request",
            vec![
                field("trusted_amount", "number", "Amount in the trusted server record"),
                field("received_amount", "number", "Amount in the received request"),
                field("received_action", "text", "Action in the received request"),
                field("added_role", "text", "Role or permission added by the received request"),
            ],
        ),
    );
    m.push_unique(
        "invoice",
        preset(
            "Invoice or quote",
            vec![
                field("vendor", "text", "Vendor or supplier name"),
                field("document_number", "text", "Invoice or quote number"),
                field("amount", "number", "Total amount"),
                field("currency", "text", "Currency code"),
                field("due_date", "date", "Due date"),
            ],
        ),
    );
    m.push_unique(
        "payment",
        preset(
            "Payment or refund request",
            vec![
                field("amount", "number", "Amount paid"),
                field("payment_date", "date", "Date of the payment"),
                field("requested_action", "text", "What the customer asks for"),
            ],
        ),
    );
    m.push_unique(
        "stock",
        preset(
            "Stock request",
            vec![field("item_code", "text", "Item code"), field("units_in_stock", "integer", "Units in stock"), field("units_needed", "integer", "Units needed")],
        ),
    );
    Value::Object(m)
}

// ------------------------------------------------------------------------------------- fields and attached records

/// `^[a-z][a-z0-9_]{0,39}$`
fn is_field_name(s: &str) -> bool { s.len() <= 40 && casefmt::is_name(s) }

/// Checked copy of a field list: `[{name, type, description?, options?}]` (`needle.validate_fields`).
pub fn validate_fields(fields: Option<&Value>) -> Result<Vec<Map>, String> {
    let items = match fields {
        Some(Value::Array(a)) if (1..=MAX_FIELDS).contains(&a.len()) => a,
        _ => return Err(format!("Declare 1-{MAX_FIELDS} fields")),
    };
    let mut out: Vec<Map> = Vec::new();
    for item in items {
        let m = item.as_object().ok_or("Each field must be an object with name and type")?;
        let name = match m.get("name") {
            Some(Value::Str(n)) if is_field_name(n) => n.clone(),
            _ => return Err("Field names: lowercase letters, digits and _, starting with a letter, up to 40 characters".into()),
        };
        if out.iter().any(|f| f.get("name").and_then(Value::as_str) == Some(&name)) {
            return Err(format!("Duplicate field: {name}"));
        }
        let kind = match m.get("type") {
            None => "text".to_owned(),
            Some(Value::Str(k)) if FIELD_TYPES.contains(&k.as_str()) => k.clone(),
            _ => return Err(format!("Field {name}: type must be one of {}", FIELD_TYPES.join(", "))),
        };
        let description = match m.get("description") {
            None | Some(Value::Null) => String::new(),
            Some(Value::Str(d)) if py_len(d) <= 300 => d.clone(),
            _ => return Err(format!("Field {name}: description up to 300 characters")),
        };
        let mut f = Map::new();
        f.push_unique("name", Value::Str(name.clone()));
        f.push_unique("type", Value::Str(kind.clone()));
        f.push_unique("description", Value::Str(strip(&description).to_owned()));
        match m.get("options") {
            None | Some(Value::Null) => {}
            Some(Value::Array(o)) if kind == "text" && (2..=MAX_OPTIONS).contains(&o.len()) => {
                let texts: Vec<&str> = o.iter().filter_map(Value::as_str).filter(|s| !strip(s).is_empty() && py_len(s) <= 100).collect();
                let unique = (0..texts.len()).all(|i| (0..i).all(|j| texts[i] != texts[j]));
                if texts.len() != o.len() || !unique {
                    return Err(format!("Field {name}: options must be 2-{MAX_OPTIONS} unique texts (text fields only)"));
                }
                if !o.is_empty() {
                    f.push_unique("options", Value::Array(o.clone()));
                }
            }
            Some(Value::Array(o)) if o.is_empty() => {}
            _ => return Err(format!("Field {name}: options must be 2-{MAX_OPTIONS} unique texts (text fields only)")),
        }
        out.push(f);
    }
    Ok(out)
}

fn field<'a>(f: &'a Map, key: &str) -> &'a str { f.get(key).and_then(Value::as_str).unwrap_or("") }

/// The tool the engine fills; no field is required, so the engine leaves out what the text does not state.
fn tool_schema(fields: &[Map]) -> Value {
    let mut properties = Map::new();
    for f in fields {
        let kind = field(f, "type");
        let mut spec = Map::new();
        spec.push_unique("type", Value::str(match kind {
            "text" | "date" => "string",
            k => k,
        }));
        let description = if field(f, "description").is_empty() { field(f, "name").replace('_', " ") } else { field(f, "description").to_owned() };
        spec.push_unique("description", Value::Str(description));
        if kind == "date" {
            spec.push_unique("format", Value::str("date"));
        }
        if let Some(o) = f.get("options") {
            spec.push_unique("enum", o.clone());
        }
        properties.push_unique(field(f, "name"), Value::Object(spec));
    }
    let mut parameters = Map::new();
    parameters.push_unique("type", Value::str("object"));
    parameters.push_unique("properties", Value::Object(properties));
    parameters.push_unique("required", Value::Array(Vec::new()));
    let mut tool = Map::new();
    tool.push_unique("name", Value::str(TOOL_NAME));
    tool.push_unique("description", Value::str(TOOL_DESCRIPTION));
    tool.push_unique("parameters", Value::Object(parameters));
    Value::Object(tool)
}

/// `needle._typed`
fn typed(value: &Value, kind: &str) -> bool {
    match (kind, value) {
        (_, Value::Null) => false,
        ("boolean", v) => matches!(v, Value::Bool(_)),
        ("integer", Value::Int(_)) => true,
        ("integer", Value::Float(f)) => f.is_finite() && f.fract() == 0.0,
        ("number", Value::Int(_)) => true,
        ("number", Value::Float(f)) => f.is_finite(),
        ("integer" | "number", _) => false,
        (_, v) => matches!(v, Value::Str(_)),
    }
}

/// Validate the `extracted_fields` object a decision request may carry (`needle.check_attached`).
pub fn check_attached(record: &Value) -> Result<Value, String> {
    let fields = match (record.as_object().and_then(|m| m.get("engine")), record.as_object().and_then(|m| m.get("fields"))) {
        (Some(Value::Str(e)), Some(Value::Array(f))) if e == "needle3" && (1..=MAX_FIELDS).contains(&f.len()) => f,
        _ => return Err("extracted_fields must be the object returned by field extraction (engine needle3, 1-12 fields)".into()),
    };
    let mut out = Vec::new();
    for f in fields {
        let m = f.as_object().ok_or("extracted_fields: invalid field entry")?;
        let name = m.get("name").and_then(Value::as_str).filter(|n| is_field_name(n));
        let kind = m.get("type").and_then(Value::as_str).filter(|k| FIELD_TYPES.contains(k));
        let check = m.get("check").and_then(Value::as_str).filter(|c| ["in_text", "not_in_text", "inferred", "not_stated"].contains(c));
        let (Some(name), Some(kind), Some(check)) = (name, kind, check) else { return Err("extracted_fields: invalid field entry".into()) };
        let value = m.get("value").cloned().unwrap_or(Value::Null);
        if !matches!(value, Value::Null) && !typed(&value, kind) || value.as_str().is_some_and(|s| py_len(s) > 1000) {
            return Err(format!("extracted_fields: invalid value for {name}"));
        }
        let mut e = Map::new();
        e.push_unique("name", Value::str(name));
        e.push_unique("type", Value::str(kind));
        e.push_unique("value", value);
        e.push_unique("check", Value::str(check));
        out.push(Value::Object(e));
    }
    let mut m = Map::new();
    m.push_unique("engine", Value::str("needle3"));
    m.push_unique("fields", Value::Array(out));
    m.push_unique("values_are_unverified", Value::Bool(true));
    m.push_unique("note", Value::str(NOTE));
    Ok(Value::Object(m))
}

/// `date: YYYY-MM-DD Dow` (resolves relative dates; such values are then marked not_in_text).
fn system_line(today: (i64, u32, u32)) -> String {
    let (y, m, d) = today;
    // days since 1970-01-01 (a Thursday)
    let days = (0..=60000i64).map(|k| k - 20000).find(|k| civil(*k) == (y, m, d)).unwrap_or(0);
    let dow = ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"][days.rem_euclid(7) as usize];
    format!("date: {y:04}-{m:02}-{d:02} {dow}")
}

// ----------------------------------------------------------------------------------------------- native engine

type LoadFn = unsafe extern "C" fn(*const c_char, u64) -> c_int;
type InitFn = unsafe extern "C" fn(*const c_char, *const c_char, *const c_char) -> c_int;
type CompleteFn = unsafe extern "C" fn(*const c_char, *mut c_float, c_int, c_int, *mut c_char, c_int) -> c_int;
type ResetFn = unsafe extern "C" fn();

struct Lib {
    init: InitFn,
    complete: CompleteFn,
    reset: ResetFn,
    /// the weights stay in memory for the life of the process (the engine may keep pointers into them)
    _weights: Vec<u8>,
}

// The engine is only ever called with the lock of `Needle::state` held.
unsafe impl Send for Lib {}

#[cfg(windows)]
mod os {
    use std::ffi::c_void;
    use std::os::windows::ffi::OsStrExt;
    use std::path::Path;
    #[link(name = "kernel32")]
    extern "system" {
        fn LoadLibraryExW(name: *const u16, file: *mut c_void, flags: u32) -> *mut c_void;
        fn GetProcAddress(module: *mut c_void, name: *const u8) -> *mut c_void;
    }
    const LOAD_WITH_ALTERED_SEARCH_PATH: u32 = 0x8;
    pub fn open(path: &Path) -> Result<*mut c_void, String> {
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        let h = unsafe { LoadLibraryExW(wide.as_ptr(), std::ptr::null_mut(), LOAD_WITH_ALTERED_SEARCH_PATH) };
        if h.is_null() { Err(format!("{} could not be loaded ({})", path.display(), std::io::Error::last_os_error())) } else { Ok(h) }
    }
    pub fn symbol(h: *mut c_void, name: &str) -> *mut c_void {
        let c = format!("{name}\0");
        unsafe { GetProcAddress(h, c.as_ptr()) }
    }
}

#[cfg(unix)]
mod os {
    use std::ffi::{c_char, c_int, c_void, CStr, CString};
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;
    #[cfg_attr(target_os = "linux", link(name = "dl"))]
    extern "C" {
        fn dlopen(file: *const c_char, mode: c_int) -> *mut c_void;
        fn dlsym(handle: *mut c_void, name: *const c_char) -> *mut c_void;
        fn dlerror() -> *const c_char;
    }
    const RTLD_NOW: c_int = 2;
    pub fn open(path: &Path) -> Result<*mut c_void, String> {
        let c = CString::new(path.as_os_str().as_bytes()).map_err(|_| "invalid library path".to_owned())?;
        let h = unsafe { dlopen(c.as_ptr(), RTLD_NOW) };
        if h.is_null() {
            let e = unsafe { dlerror() };
            let why = if e.is_null() { String::new() } else { unsafe { CStr::from_ptr(e) }.to_string_lossy().into_owned() };
            Err(format!("{} could not be loaded ({why})", path.display()))
        } else {
            Ok(h)
        }
    }
    pub fn symbol(h: *mut c_void, name: &str) -> *mut c_void {
        let c = CString::new(name).unwrap_or_default();
        unsafe { dlsym(h, c.as_ptr()) }
    }
}

fn load_library(path: &Path, weights: Vec<u8>) -> Result<Lib, String> {
    let h = os::open(path)?;
    let sym = |name: &str| -> Result<*mut c_void, String> {
        let p = os::symbol(h, name);
        if p.is_null() { Err(format!("{} has no {name}", path.display())) } else { Ok(p) }
    };
    // SAFETY: the library is the pinned engine (hash checked before loading) with the C signatures above.
    let (load, init, complete, reset) = unsafe {
        (
            std::mem::transmute::<*mut c_void, LoadFn>(sym("needle_load")?),
            std::mem::transmute::<*mut c_void, InitFn>(sym("needle_init")?),
            std::mem::transmute::<*mut c_void, CompleteFn>(sym("needle_complete")?),
            std::mem::transmute::<*mut c_void, ResetFn>(sym("needle_reset")?),
        )
    };
    if unsafe { load(weights.as_ptr() as *const c_char, weights.len() as u64) } != 0 {
        return Err(format!("The Needle engine could not read {WEIGHTS}"));
    }
    Ok(Lib { init, complete, reset, _weights: weights })
}

#[derive(Default)]
struct State {
    lib: Option<Lib>,
    error: Option<String>,
    tools: Option<String>,
}

/// The engine, loaded on first use from verified local files.
pub struct Needle {
    pub dir: PathBuf,
    /// the `needle` entry of config/models.lock.json (None: the lock has no Needle entry)
    lock: Option<Map>,
    state: Mutex<State>,
}

impl Needle {
    pub fn new(dir: PathBuf, lock: Option<Map>) -> Needle { Needle { dir, lock, state: Mutex::new(State::default()) } }

    /// models/needle (or TYPELOGIC_NEEDLE_DIR) and the lock entry of `config/models.lock.json`.
    pub fn discover(models: &Path, config: &Path) -> Needle {
        let dir = std::env::var("TYPELOGIC_NEEDLE_DIR").ok().filter(|v| !v.trim().is_empty()).map(PathBuf::from).unwrap_or_else(|| models.join("needle"));
        let lock = std::fs::read_to_string(config.join("models.lock.json"))
            .ok()
            .and_then(|t| loads(&t, false).ok())
            .and_then(|v| v.as_object().and_then(|m| m.get("needle")).and_then(Value::as_object).cloned());
        Needle::new(dir, lock)
    }

    fn files(&self) -> (PathBuf, PathBuf) { (self.dir.join(LIBRARY), self.dir.join(WEIGHTS)) }

    pub fn available(&self) -> bool {
        let (lib, weights) = self.files();
        self.lock.is_some() && lib.is_file() && weights.is_file() && self.state.lock().unwrap_or_else(|e| e.into_inner()).error.is_none()
    }

    pub fn status(&self) -> Value {
        let (lib, weights) = self.files();
        let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let reason = if self.lock.is_none() {
            Some("config/models.lock.json has no Needle entry".to_owned())
        } else if !lib.is_file() || !weights.is_file() {
            Some("Needle is not installed (run: TypeLogic setup)".to_owned())
        } else {
            state.error.clone()
        };
        let mut limits = Map::new();
        limits.push_unique("max_text_chars", Value::int(MAX_TEXT_CHARS as i64));
        limits.push_unique("max_fields", Value::int(MAX_FIELDS as i64));
        limits.push_unique("evidence_max_chars", Value::int(SHORT_TEXT_CHARS as i64));
        let mut m = Map::new();
        m.push_unique("engine", Value::str("needle3"));
        m.push_unique("folder", Value::Str(self.dir.display().to_string()));
        m.push_unique("available", Value::Bool(reason.is_none()));
        m.push_unique("reason", reason.map(Value::Str).unwrap_or(Value::Null));
        m.push_unique("loaded", Value::Bool(state.lib.is_some()));
        m.push_unique("limits", Value::Object(limits));
        m.push_unique("types", Value::Array(FIELD_TYPES.iter().map(|t| Value::str(*t)).collect()));
        m.push_unique("presets", presets());
        m.push_unique("network", Value::str("none (native engine only; the needle Python package is not used)"));
        Value::Object(m)
    }

    fn verify(&self) -> Result<(), String> {
        let lock = self.lock.as_ref().ok_or("config/models.lock.json has no Needle entry")?;
        let (lib, weights) = self.files();
        let weights_sha = lock.get("weights").and_then(Value::as_object).and_then(|w| w.get("sha256")).and_then(Value::as_str).unwrap_or("");
        if sha256_file(&weights).map_err(|e| e.to_string())? != weights_sha {
            return Err(format!("{WEIGHTS} does not match config/models.lock.json (run: TypeLogic setup)"));
        }
        let known: Vec<String> = lock
            .get("platforms")
            .and_then(Value::as_object)
            .map(|p| p.iter().filter_map(|(_, v)| v.as_object().and_then(|m| m.get("sha256")).and_then(Value::as_str).map(str::to_owned)).collect())
            .unwrap_or_default();
        if !known.contains(&sha256_file(&lib).map_err(|e| e.to_string())?) {
            return Err(format!("{LIBRARY} does not match config/models.lock.json (run: TypeLogic setup)"));
        }
        Ok(())
    }

    fn complete(&self, state: &mut State, system: &str, tools: &str, text: &str) -> Result<Value, String> {
        if state.lib.is_none() {
            let loaded = self.verify().and_then(|_| {
                let (lib, weights) = self.files();
                let data = std::fs::read(&weights).map_err(|e| format!("{WEIGHTS}: {e}"))?;
                load_library(&lib, data)
            });
            match loaded {
                Ok(l) => state.lib = Some(l),
                Err(e) => {
                    let e: String = e.chars().take(300).collect();
                    state.error = Some(e.clone());
                    return Err(e);
                }
            }
        }
        let lib = state.lib.as_ref().unwrap();
        let c = |s: &str| CString::new(s).map_err(|_| "text with a NUL character".to_owned());
        if state.tools.as_deref() != Some(tools) {
            let (s, t) = (c(system)?, c(tools)?);
            if unsafe { (lib.init)(s.as_ptr(), t.as_ptr(), std::ptr::null()) } < 0 {
                return Err("The Needle engine rejected the field list".into());
            }
            state.tools = Some(tools.to_owned());
        } else {
            unsafe { (lib.reset)() };
        }
        let input = c(text)?;
        let mut size: usize = 1 << 16;
        let (rc, raw) = loop {
            let mut buffer = vec![0u8; size];
            let rc = unsafe { (lib.complete)(input.as_ptr(), std::ptr::null_mut(), 0, MAX_NEW_TOKENS, buffer.as_mut_ptr() as *mut c_char, size as c_int) };
            let end = buffer.iter().position(|b| *b == 0).unwrap_or(buffer.len());
            let raw = String::from_utf8_lossy(&buffer[..end]).into_owned();
            if rc < 0 && raw.is_empty() && size < 1 << 22 {
                size *= 4; // the output did not fit the buffer
                unsafe { (lib.reset)() };
                continue;
            }
            break (rc, raw);
        };
        let result = loads(&raw, false).map_err(|_| "The Needle engine returned invalid output".to_owned())?;
        unsafe { (lib.reset)() };
        let kind = result.as_object().and_then(|m| m.get("type")).and_then(Value::as_str);
        if rc < 0 || kind == Some("error") {
            state.tools = None;
            let why = match result.as_object().and_then(|m| m.get("error")) {
                Some(Value::Str(s)) if !s.is_empty() => s.clone(),
                Some(v) if !matches!(v, Value::Null) && v.truthy() => dumps(v, DumpOptions::REPLY).unwrap_or_default(),
                _ => format!("error code {rc}"),
            };
            return Err(format!("Needle: {}", why.chars().take(200).collect::<String>()));
        }
        Ok(result)
    }

    /// `needle.extract(text, fields)`: the fields the text states, each checked against the text.
    pub fn extract(&self, text: Option<&Value>, fields: Option<&Value>) -> Result<Value, Extract> {
        let text = match text {
            Some(Value::Str(t)) if !strip(t).is_empty() => t.clone(),
            _ => return Err(Extract::Invalid("Provide the scenario text to read".into())),
        };
        let n = py_len(&text);
        if n > MAX_TEXT_CHARS {
            return Err(Extract::Invalid(format!(
                "Field extraction reads up to {} characters; this text has {}. Shorten it or extract from a part of it; the text is never cut automatically.",
                casefmt::thousands(MAX_TEXT_CHARS),
                casefmt::thousands(n)
            )));
        }
        let fields = validate_fields(fields).map_err(Extract::Invalid)?;
        let system = system_line(today_local());
        let tools = dumps(&Value::Array(vec![tool_schema(&fields)]), DumpOptions::REPLY).map_err(|e| Extract::Engine(e.message))?;
        let started = Instant::now();
        let result = {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            self.complete(&mut state, &system, &tools, &text).map_err(Extract::Engine)?
        };
        let seconds = started.elapsed().as_secs_f64();
        let get = |k: &str| result.as_object().and_then(|m| m.get(k));
        let calls = get("function_calls").and_then(Value::as_array).cloned().unwrap_or_default();
        let suppressed = get("suppressed_calls").and_then(Value::as_array).cloned().unwrap_or_default();
        let withheld = calls.is_empty() && !suppressed.is_empty();
        let pool = if calls.is_empty() { &suppressed } else { &calls };
        let arguments = pool
            .iter()
            .find(|c| c.as_object().and_then(|m| m.get("name")).and_then(Value::as_str) == Some(TOOL_NAME))
            .and_then(|c| c.as_object().and_then(|m| m.get("arguments")).and_then(Value::as_object).cloned())
            .unwrap_or_default();
        let mut out = Vec::new();
        for f in &fields {
            let kind = field(f, "type");
            let mut value = arguments.get(field(f, "name")).cloned().unwrap_or(Value::Null);
            if kind == "integer" {
                if let Value::Float(x) = value {
                    if x.is_finite() && x.fract() == 0.0 && x.abs() < 1e18 {
                        value = Value::int(x as i64);
                    }
                }
            }
            let allowed = f.get("options").and_then(Value::as_array).is_none_or(|o| o.contains(&value));
            if !typed(&value, kind) || !allowed || value.as_str().is_some_and(|s| strip(s).is_empty()) {
                value = Value::Null;
            }
            let check = if matches!(value, Value::Null) { "not_stated" } else { evidence::check(&value, kind, &text) };
            let mut e = Map::new();
            e.push_unique("name", Value::str(field(f, "name")));
            e.push_unique("type", Value::str(kind));
            e.push_unique("value", value);
            e.push_unique("check", Value::str(check));
            out.push(Value::Object(e));
        }
        let confidence = match get("confidence") {
            Some(v @ (Value::Int(_) | Value::Float(_))) => v.to_f64().ok().map(|c| Value::Float((c * 1e4).round() / 1e4)).unwrap_or(Value::Null),
            _ => Value::Null,
        };
        let reasoning = match get("reasoning") {
            Some(Value::Str(s)) => s.chars().take(2000).collect(),
            Some(Value::Null) | None => String::new(),
            Some(v) => dumps(v, DumpOptions::REPLY).unwrap_or_default().chars().take(2000).collect(),
        };
        let mut m = Map::new();
        m.push_unique("engine", Value::str("needle3"));
        m.push_unique("fields", Value::Array(out));
        m.push_unique("chars", Value::int(n as i64));
        m.push_unique("seconds", Value::Float((seconds * 1000.0).round() / 1000.0));
        m.push_unique("confidence", confidence);
        m.push_unique("withheld_by_engine", Value::Bool(withheld));
        m.push_unique("reasoning", Value::Str(reasoning));
        m.push_unique("engine_flags", get("validation").filter(|v| v.truthy()).cloned().unwrap_or(Value::Object(Map::new())));
        m.push_unique("note", Value::str(NOTE));
        Ok(Value::Object(m))
    }

    /// Evidence extraction through Needle (`evidence._needle_extract`): the evidence schema as Needle fields
    /// (enum -> text with options), short texts only. -> (values by evidence field name, confidence)
    pub fn evidence_values(&self, text: &str, schema: &[Map]) -> Result<(Map, Value), Extract> {
        if py_len(text) > SHORT_TEXT_CHARS {
            return Err(Extract::Invalid(format!(
                "Needle extracts from short texts only (up to {} characters); use the local LLM (Plumb) for this text",
                casefmt::thousands(SHORT_TEXT_CHARS)
            )));
        }
        if schema.len() > MAX_FIELDS {
            return Err(Extract::Invalid(format!("Needle reads up to {MAX_FIELDS} fields at a time")));
        }
        let fields: Vec<Value> = schema
            .iter()
            .map(|f| {
                let name: String = field(f, "name").chars().take(40).collect();
                let enumerated = field(f, "type") == "enum";
                let mut m = Map::new();
                m.push_unique("name", Value::Str(name));
                m.push_unique("type", Value::str(if enumerated { "text" } else { field(f, "type") }));
                m.push_unique("description", Value::str(field(f, "description")));
                if enumerated {
                    m.push_unique("options", f.get("options").cloned().unwrap_or(Value::Null));
                }
                Value::Object(m)
            })
            .collect();
        let result = self.extract(Some(&Value::str(text)), Some(&Value::Array(fields)))?;
        let mut values = Map::new();
        for (spec, row) in schema.iter().zip(result.as_object().and_then(|m| m.get("fields")).and_then(Value::as_array).cloned().unwrap_or_default()) {
            values.push_unique(field(spec, "name"), row.as_object().and_then(|m| m.get("value")).cloned().unwrap_or(Value::Null));
        }
        let confidence = result.as_object().and_then(|m| m.get("confidence")).cloned().unwrap_or(Value::Null);
        Ok((values, confidence))
    }
}

/// An extraction failure: a request to fix (422) or the engine (503).
#[derive(Debug)]
pub enum Extract {
    Invalid(String),
    Engine(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fields(json: &str) -> Result<Vec<Map>, String> { validate_fields(Some(&loads(json, false).unwrap())) }

    #[test]
    fn validates_fields_like_1x() {
        let ok = fields(r#"[{"name": "amount", "type": "number"}, {"name": "kind", "options": ["a", "b"], "description": " The kind "}]"#).unwrap();
        assert_eq!(field(&ok[1], "type"), "text");
        assert_eq!(field(&ok[1], "description"), "The kind");
        assert_eq!(fields("[]").unwrap_err(), "Declare 1-12 fields");
        assert!(fields(r#"[{"name": "Amount"}]"#).unwrap_err().starts_with("Field names"));
        assert_eq!(fields(r#"[{"name": "a"}, {"name": "a"}]"#).unwrap_err(), "Duplicate field: a");
        assert!(fields(r#"[{"name": "a", "type": "enum"}]"#).unwrap_err().contains("type must be one of"));
        assert!(fields(r#"[{"name": "a", "type": "number", "options": ["x", "y"]}]"#).unwrap_err().contains("options"));
        assert!(fields(r#"[{"name": "a", "options": ["x", "x"]}]"#).unwrap_err().contains("unique"));
    }

    #[test]
    fn tool_schema_matches_1x() {
        // json.dumps([tool_schema(validate_fields(...))], ensure_ascii=False) in 1.x
        let f = fields(r#"[{"name": "due_date", "type": "date"}, {"name": "kind", "options": ["a", "b"], "description": "Kind"}]"#).unwrap();
        let text = dumps(&Value::Array(vec![tool_schema(&f)]), DumpOptions::REPLY).unwrap();
        assert_eq!(
            text,
            r#"[{"name": "record_fields", "description": "Record the requested fields exactly as stated in the text. Leave out any field the text does not state.", "parameters": {"type": "object", "properties": {"due_date": {"type": "string", "description": "due date", "format": "date"}, "kind": {"type": "string", "description": "Kind", "enum": ["a", "b"]}}, "required": []}}]"#
        );
    }

    #[test]
    fn checks_attached_records() {
        let ok = check_attached(&loads(r#"{"engine": "needle3", "fields": [{"name": "amount", "type": "number", "value": 12.5, "check": "in_text"}, {"name": "n", "type": "integer", "value": 3.0, "check": "in_text"}]}"#, false).unwrap()).unwrap();
        assert!(dumps(&ok, DumpOptions::REPLY).unwrap().contains("\"values_are_unverified\": true"));
        assert!(check_attached(&loads(r#"{"engine": "other", "fields": []}"#, false).unwrap()).is_err());
        assert!(check_attached(&loads(r#"{"engine": "needle3", "fields": [{"name": "a", "type": "number", "value": "x", "check": "in_text"}]}"#, false).unwrap()).unwrap_err().contains("invalid value for a"));
    }

    #[test]
    fn system_line_has_the_weekday() {
        assert_eq!(system_line((2026, 10, 29)), "date: 2026-10-29 Thu");
        assert_eq!(system_line((1970, 1, 1)), "date: 1970-01-01 Thu");
        assert_eq!(system_line((2024, 2, 29)), "date: 2024-02-29 Thu");
    }

    /// The FFI path against tests/gen/fake_needle.c built into $TYPELOGIC_FAKE_NEEDLE (with needle3.cact = "WEIGHTS!").
    #[test]
    #[ignore]
    fn calls_the_native_engine() {
        let dir = PathBuf::from(std::env::var("TYPELOGIC_FAKE_NEEDLE").expect("TYPELOGIC_FAKE_NEEDLE"));
        let sha = |f: &str| Value::Str(sha256_file(&dir.join(f)).unwrap());
        let mut weights = Map::new();
        weights.push_unique("sha256", sha(WEIGHTS));
        let mut lib = Map::new();
        lib.push_unique("sha256", sha(LIBRARY));
        let mut platforms = Map::new();
        platforms.push_unique("this", Value::Object(lib));
        let mut lock = Map::new();
        lock.push_unique("weights", Value::Object(weights));
        lock.push_unique("platforms", Value::Object(platforms));
        let n = Needle::new(dir.clone(), Some(lock.clone()));
        assert!(n.available());
        let spec = loads(r#"[{"name": "amount", "type": "number"}, {"name": "days", "type": "integer"}, {"name": "kind", "options": ["refund", "other"]}, {"name": "missing"}]"#, false).unwrap();
        let text = Value::str("Please refund 12.5 after 3 days.");
        let first = n.extract(Some(&text), Some(&spec)).unwrap();
        let out = dumps(&first, DumpOptions::REPLY).unwrap();
        assert!(out.contains(r#"{"name": "amount", "type": "number", "value": 12.5, "check": "in_text"}"#), "{out}");
        assert!(out.contains(r#"{"name": "days", "type": "integer", "value": 3, "check": "in_text"}"#), "{out}");
        assert!(out.contains(r#"{"name": "kind", "type": "text", "value": "refund", "check": "in_text"}"#), "{out}");
        assert!(out.contains(r#"{"name": "missing", "type": "text", "value": null, "check": "not_stated"}"#), "{out}");
        assert!(out.contains(r#""confidence": 0.9123"#) && out.contains(r#""reasoning": "inits=1 resets=1""#), "{out}");
        // same fields: no new init, a reset before the call
        let again = dumps(&n.extract(Some(&text), Some(&spec)).unwrap(), DumpOptions::REPLY).unwrap();
        assert!(again.contains("inits=1 resets=4"), "{again}");
        // a library that does not match the lock is refused before it is loaded
        let mut bad = lock.clone();
        bad.insert("weights", loads(r#"{"sha256": "00"}"#, false).unwrap());
        let refused = Needle::new(dir, Some(bad));
        assert!(matches!(refused.extract(Some(&text), Some(&spec)), Err(Extract::Engine(e)) if e.contains("does not match")));
        assert!(!refused.available());
    }

    #[test]
    fn missing_files_are_reported() {
        let n = Needle::new(std::env::temp_dir().join("typelogic-no-needle"), Some(Map::new()));
        assert!(!n.available());
        let s = n.status();
        assert!(dumps(&s, DumpOptions::REPLY).unwrap().contains("Needle is not installed"));
    }
}
